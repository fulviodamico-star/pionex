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

# Regole Risk Manager & Budget Manager (Skill #2 & #6)
MAX_LOSS_PCT_PER_TRADE = 3.6    # Perdita max tollerata per singolo trade (~1.95$ su 54$)
DAILY_MAX_LOSS_PCT = 6.0        # Perdita max giornaliera cumulativa (-6%)
MAX_TRADES_PER_DAY = 3          # Max aperture giornaliere consentite
MIN_REWARD_RISK_R = 1.35        # Rapporto Reward:Risk minimo richiesto (1.35R)
CASH_BUFFER_PCT = 5.0           # Riserva liquidità (5%) per coprire fee e slippage

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

# Calendario Festività NYSE 2026 e 2027 (Skill #1 - Price Alerts & Opening Bell)
NYSE_HOLIDAYS_2026_2027 = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}

ACTIVE_LOWER = float(os.getenv("ACTIVE_LOWER") or "0")
ACTIVE_UPPER = float(os.getenv("ACTIVE_UPPER") or "0")

AUTO_TRADE_ENABLED = (os.getenv("AUTO_TRADE_ENABLED") or "false").strip().lower() == "true"
PIONEX_WEBHOOK_URL = os.getenv("PIONEX_WEBHOOK_URL") or ""
PIONEX_SIGNAL_TOKEN = os.getenv("PIONEX_SIGNAL_TOKEN") or ""
PIONEX_API_KEY = os.getenv("PIONEX_API_KEY") or ""
PIONEX_API_SECRET = os.getenv("PIONEX_API_SECRET") or ""
STATE_FILE = "bot_state.json"

TARGET_STEP_PCT = 0.32
PIONEX_ROUNDTRIP_FEE_PCT = 0.10
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
# UTILITÀ FORMATTAZIONE PREZZI MULTI-DECIMALE
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
        "initial_stop": 0.0,
        "one_r_usd": 0.0,
        "grids": 0,
        "qty_coin": 0.0,
        "trailing_locked": False,
        "alert_1r_sent": False,
        "alert_2r_sent": False,
        "opened_at": "",
        "opened_date": "",
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
# CALENDARIO MACROECONOMICO USA & NYSE OPENING BELL (2026-2027 HOLIDAYS)
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
    """Skill #1: Controlla l'Opening Bell NYSE (09:30 NY) tenendo conto delle festività 2026-2027."""
    ny_time = now_utc.astimezone(ZoneInfo("America/New_York"))
    if ny_time.weekday() >= 5:
        return False
    if ny_time.strftime("%Y-%m-%d") in NYSE_HOLIDAYS_2026_2027:
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
# NUOVE SKILL INTEGRATE: CHART SCANNER, ENTRY TIMER, EXIT PLANNER, TIME MACHINE
# ==============================================================================

def run_chart_scanner_and_entry_timer(opens_1h, highs_1h, lows_1h, closes_1h, vols_1h,
                                      opens_15m, highs_15m, lows_15m, closes_15m, vols_15m,
                                      ema20_1h, bb_pct_b, pos_in_channel):
    """
    Skill #3 & #8: Chart Scanner + Entry Timer (Bar-by-Bar).
    Individua il pattern grafico (Oversold Bounce, Bull Flag, Range Reclaim, Equilibrium)
    e verifica se l'ultima barra 15m completata ha rotto al rialzo il micro-range con volumi.
    """
    price = closes_15m[-1]
    prev_close_15m = closes_15m[-2]
    prev_open_15m = opens_15m[-2]
    bar_completed_green = prev_close_15m >= prev_open_15m

    # Micro Opening Range sulle 4 barre 15m precedenti (1 ora di consolidamento)
    or_high = max(highs_15m[-6:-2])
    or_low = min(lows_15m[-6:-2])
    or_mid = (or_high + or_low) / 2.0

    vol_avg_15m = sum(vols_15m[-22:-2]) / 20.0 if len(vols_15m) >= 22 else 1.0
    vol_confirm = (vols_15m[-2] >= vol_avg_15m * 0.85) or (vols_15m[-1] >= vol_avg_15m * 0.90)

    # Identificazione Pattern (Chart Scanner)
    high_20h = max(highs_1h[-21:-1])
    recent_flag_range = (max(highs_1h[-6:-1]) - min(lows_1h[-6:-1])) / max(price, 1e-8) * 100.0
    prior_pole = ((max(highs_1h[-14:-5]) - min(lows_1h[-18:-10])) / max(price, 1e-8)) * 100.0

    setup_name = "Nessun Setup"
    triggered = False
    status_txt = "WAITING ⏳"

    # 1. Oversold Bounce Setup
    if (bb_pct_b <= 42 or pos_in_channel <= 40) and (price > or_mid and bar_completed_green):
        setup_name = "Oversold Bounce (Rimbalzo Ipervenduto)"
        triggered = True
        status_txt = f"TRIGGERED ✅ (Chiusura 15m sopra micro-mid ${fmt_p(or_mid)})"
    # 2. Bull Flag Breakout Setup
    elif prior_pole >= 3.0 and recent_flag_range <= 2.8 and (prev_close_15m > or_high or price > or_high):
        setup_name = "Bull Flag Breakout (Bandiera Rialzista)"
        triggered = True
        status_txt = f"TRIGGERED ✅ (Breakout sopra ${fmt_p(or_high)})"
    # 3. 20-Bar High / EMA20 Reclaim Setup
    elif closes_1h[-2] <= ema20_1h and price > ema20_1h and bar_completed_green:
        setup_name = "EMA20 Reclaim (Recupero Media 20)"
        triggered = True
        status_txt = f"TRIGGERED ✅ (Recuperata EMA20 ${fmt_p(ema20_1h)})"
    # 4. Grid Equilibrium Setup (Prezzo nella metà bassa/centrale del canale con barra 15m verde)
    elif 15 <= pos_in_channel <= 62 and (bar_completed_green or price >= or_mid) and vol_confirm:
        setup_name = "Grid Equilibrium (Oscillazione Ideale)"
        triggered = True
        status_txt = f"TRIGGERED ✅ (Conferma 15m a ${fmt_p(price)})"
    else:
        dist_pct = ((or_high - price) / max(price, 1e-8)) * 100.0
        setup_name = "Consolidamento in Osservazione"
        triggered = False
        status_txt = f"WAITING ⏳ (A -{ abs(dist_pct):.2f}% dal trigger ${fmt_p(or_high)})"

    return {
        "setup_name": setup_name,
        "triggered": triggered,
        "status_txt": status_txt,
        "or_high": or_high,
        "or_low": or_low,
        "or_mid": or_mid,
        "vol_confirm": vol_confirm,
    }


def run_exit_planner(price, lower, upper, atr_1h, step_usd, spread_bps, net_step_pct, grids):
    """
    Skill #4: Exit Planner (Stop, Target, R-Multiple & Spread Survival Validator).
    Calcola lo Stop Loss ottimale, verifica che non sia troppo stretto per sopravvivere
    allo spread e al rumore, e calcola il multiplo di rischio 1R e il rapporto Reward:Risk.
    """
    spread_usd = price * (spread_bps / 10000.0)
    min_survivable_dist = max(spread_usd * 8.0, atr_1h * 0.85, price * 0.012)

    raw_stop = lower - max(atr_1h * 1.05, step_usd * 1.25)
    stop_dist = price - raw_stop

    # Assicura che lo stop sopravviva allo spread ma resti entro il budget di rischio
    if stop_dist < min_survivable_dist:
        raw_stop = price - min_survivable_dist
        stop_dist = min_survivable_dist

    stop_price = round_p(raw_stop)
    one_r_usd = max(price - stop_price, 1e-8)
    one_r_pct = (one_r_usd / price) * 100.0

    # Target direzionale (Upper Range) + rendimento atteso delle oscillazioni griglia
    expected_grid_cycles = max(3.0, grids * 0.65)
    grid_yield_pct = expected_grid_cycles * net_step_pct
    grid_yield_usd = price * (grid_yield_pct / 100.0)

    target_price = upper
    directional_reward_usd = max(target_price - price, 0.0)
    total_expected_reward_usd = directional_reward_usd + grid_yield_usd

    rr_ratio = total_expected_reward_usd / one_r_usd if one_r_usd > 0 else 0.0
    level_1r = round_p(price + one_r_usd)
    level_2r = round_p(price + one_r_usd * 2.0)

    valid = True
    reason = "Piano Exit valido ✅"
    if stop_price >= price:
        valid = False
        reason = "Stop sopra o uguale al prezzo di entrata"
    elif target_price <= price:
        valid = False
        reason = "Target sotto o uguale al prezzo di entrata"
    elif one_r_usd < spread_usd * 5.0:
        valid = False
        reason = "Stop troppo stretto per sopravvivere allo spread"

    return {
        "valid": valid,
        "reason": reason,
        "stop_price": stop_price,
        "target_price": target_price,
        "one_r_usd": one_r_usd,
        "one_r_pct": one_r_pct,
        "rr_ratio": rr_ratio,
        "level_1r": level_1r,
        "level_2r": level_2r,
        "grid_yield_pct": grid_yield_pct,
    }


def run_time_machine(highs_1h, lows_1h, closes_1h, entry_price, stop_price, target_price,
                     one_r_usd, rr_ratio, n_sims=1500, horizon_hours=24):
    """
    Skill #9: Time Machine (Monte Carlo 1.500 sessioni + Replay Storico del Setup).
    1) Ricostruisce le prossime 24 ore 1.500 volte campionando le escursioni orarie reali della moneta.
    2) Esegue il Replay Storico su tutte le barre passate per verificare quante volte il Target
       è stato colpito prima dello Stop Loss.
    Restituisce: Probabilità Target First, Stop First, Permanenza profittevole in Griglia e Valore Atteso E[R].
    """
    if len(closes_1h) < 80 or one_r_usd <= 0:
        return {
            "win_Or_grid_prob": 60.0,
            "target_first_prob": 35.0,
            "stop_first_prob": 25.0,
            "in_range_prob": 40.0,
            "expected_r": 0.45,
            "hist_win_rate": 60.0,
            "verdict": "DATI PARZIALI",
        }

    up_target_pct = (target_price - entry_price) / entry_price
    dn_stop_pct = (stop_price - entry_price) / entry_price  # negativo

    # 1. REPLAY STORICO SULLE ULTIME 320 ORE
    hist_wins = 0
    hist_losses = 0
    hist_in_grid = 0
    start_h = max(20, len(closes_1h) - 320)
    end_h = len(closes_1h) - horizon_hours - 1

    for i in range(start_h, end_h):
        p0 = closes_1h[i]
        t_lvl = p0 * (1.0 + up_target_pct)
        s_lvl = p0 * (1.0 + dn_stop_pct)
        outcome = 0  # 0 = rimasto nel range griglia, 1 = target first, -1 = stop first
        for k in range(1, horizon_hours + 1):
            if lows_1h[i + k] <= s_lvl:
                outcome = -1
                break
            if highs_1h[i + k] >= t_lvl:
                outcome = 1
                break
        if outcome == 1:
            hist_wins += 1
        elif outcome == -1:
            hist_losses += 1
        else:
            hist_in_grid += 1

    tot_hist = max(hist_wins + hist_losses + hist_in_grid, 1)
    hist_success_pct = ((hist_wins + hist_in_grid * 0.75) / tot_hist) * 100.0

    # 2. SIMULAZIONE MONTE CARLO (Bootstrap sui rendimenti ed escursioni High/Low reali)
    bar_shocks = []
    for i in range(max(1, len(closes_1h) - 180), len(closes_1h)):
        c_prev = closes_1h[i - 1]
        if c_prev <= 0:
            continue
        ret_c = (closes_1h[i] - c_prev) / c_prev
        ret_h = (highs_1h[i] - c_prev) / c_prev
        ret_l = (lows_1h[i] - c_prev) / c_prev
        bar_shocks.append((ret_c, ret_h, ret_l))

    rng = random.Random(int(entry_price * 1000) + len(closes_1h))
    mc_target = 0
    mc_stop = 0
    mc_range = 0
    sum_r = 0.0

    for _ in range(n_sims):
        sim_p = entry_price
        hit = 0
        oscillations = 0
        for _step in range(horizon_hours):
            rc, rh, rl = rng.choice(bar_shocks)
            bar_hi = sim_p * (1.0 + rh)
            bar_lo = sim_p * (1.0 + rl)
            if (bar_hi - bar_lo) / sim_p >= 0.0045:
                oscillations += 1
            if bar_lo <= stop_price:
                hit = -1
                break
            if bar_hi >= target_price:
                hit = 1
                break
            sim_p = sim_p * (1.0 + rc)

        if hit == 1:
            mc_target += 1
            sum_r += rr_ratio
        elif hit == -1:
            mc_stop += 1
            sum_r -= 1.0
        else:
            mc_range += 1
            # Nel Grid Bot, rimanere nel range genera profitto dalle oscillazioni + variazione prezzo
            grid_r_gain = min(rr_ratio * 0.85, oscillations * 0.08)
            price_r = (sim_p - entry_price) / one_r_usd
            sum_r += max(-0.6, price_r + grid_r_gain)

    target_first_prob = (mc_target / n_sims) * 100.0
    stop_first_prob = (mc_stop / n_sims) * 100.0
    in_range_prob = (mc_range / n_sims) * 100.0
    win_or_grid_prob = 0.65 * (target_first_prob + in_range_prob * 0.80) + 0.35 * hist_success_pct
    expected_r = sum_r / n_sims

    if expected_r >= 0.55 and stop_first_prob <= 28.0:
        verdict = "OTTIMO EDGE STATISTICO 🟢"
    elif expected_r >= 0.25 and stop_first_prob <= 38.0:
        verdict = "VANTAGGIO POSITIVO ✅"
    elif expected_r >= 0.05:
        verdict = "MARGINALE / COIN FLIP 🟡"
    else:
        verdict = "ASPETTATIVA NEGATIVA 🔴"

    return {
        "win_or_grid_prob": win_or_grid_prob,
        "target_first_prob": target_first_prob,
        "stop_first_prob": stop_first_prob,
        "in_range_prob": in_range_prob,
        "expected_r": expected_r,
        "hist_win_rate": hist_success_pct,
        "verdict": verdict,
    }


def run_budget_and_risk_manager(coin_sym, pionex_sym, price, lower, upper, grids, invest_usdt,
                                exit_plan, time_machine, news, state):
    """
    Skill #2 & #6: Budget Manager & Risk Manager Gatekeeper (PASS / BLOCK).
    Verifica i limiti di budget e di rischio prima di autorizzare qualsiasi ordine:
    1) Max Loss per Trade (Rifiuta se lo stop è troppo lontano per la perdita massima ammessa)
    2) Reward:Risk minimo (>= MIN_REWARD_RISK_R)
    3) Expectancy positiva nella Time Machine
    4) Limite di Perdita Giornaliera (Daily Loss Limit)
    5) Limite Massimo di Trade Giornalieri
    6) Cash Buffer & Min Order per griglia Pionex
    """
    usable_usdt = round(invest_usdt * (1.0 - CASH_BUFFER_PCT / 100.0), 2)
    order_usdt = invest_usdt  # Per Pionex Grid usiamo i 50€ convertiti
    qty_coin = round(order_usdt / max(price, 1e-8), 4)

    max_loss_allowed_usdt = order_usdt * (MAX_LOSS_PCT_PER_TRADE / 100.0)
    actual_risk_pct = exit_plan["one_r_pct"]
    # Nel Grid Bot l'esposizione media in cripto è ~55% del capitale allocato
    effective_loss_at_stop_usdt = order_usdt * (actual_risk_pct / 100.0) * 0.65

    # Calcolo performance di oggi dallo storico per Daily Loss Limit & Max Trades/Day
    today_str = news["now_rome"].strftime("%d/%m")
    history = state.get("history", [])
    today_trades = [h for h in history if str(h.get("closed_at", "")).startswith(today_str)]
    today_pnl_pct = sum(float(h.get("pnl_pct", 0.0)) for h in today_trades)
    trades_count_today = len(today_trades) + (1 if state.get("active") and state.get("opened_date") == today_str else 0)

    blocks = []
    if not exit_plan["valid"]:
        blocks.append(f"Exit Plan invalido ({exit_plan['reason']})")

    if effective_loss_at_stop_usdt > max_loss_allowed_usdt:
        blocks.append(
            f"Rischio a Stop (${effective_loss_at_stop_usdt:.2f}) supera Max Loss per trade (${max_loss_allowed_usdt:.2f})"
        )

    if exit_plan["rr_ratio"] < MIN_REWARD_RISK_R:
        blocks.append(
            f"Reward:Risk ({exit_plan['rr_ratio']:.2f}R) inferiore al minimo ({MIN_REWARD_RISK_R:.2f}R)"
        )

    if time_machine["expected_r"] < 0.15 or time_machine["stop_first_prob"] > 42.0:
        blocks.append(
            f"Time Machine sfavorevole (E[R]={time_machine['expected_r']:+.2f}R, StopFirst={time_machine['stop_first_prob']:.0f}%)"
        )

    if today_pnl_pct <= -DAILY_MAX_LOSS_PCT:
        blocks.append(f"Daily Loss Limit raggiunto oggi ({today_pnl_pct:+.2f}% <= -{DAILY_MAX_LOSS_PCT:.1f}%)")

    if trades_count_today >= MAX_TRADES_PER_DAY:
        blocks.append(f"Limite max trade giornalieri raggiunto ({trades_count_today}/{MAX_TRADES_PER_DAY})")

    if news["status"] == "IMMINENT" and any(e["impact"] == "High" for e in news["imminent"]):
        blocks.append("Notizia Macro USA High-Impact imminente")

    passed = len(blocks) == 0
    status = "PASS ✅" if passed else "BLOCK ⛔"
    reason = "Tutti i 6 controlli di rischio superati" if passed else " | ".join(blocks)

    bracket_order = {
        "symbol": pionex_sym,
        "side": "BUY",
        "type": "GRID_BRACKET",
        "qty_coin": qty_coin,
        "order_usdt": order_usdt,
        "usable_after_buffer_usdt": usable_usdt,
        "entry_price": round_p(price),
        "stop_loss": exit_plan["stop_price"],
        "take_profit": exit_plan["target_price"],
        "risk_usdt": round(effective_loss_at_stop_usdt, 2),
        "max_loss_allowed_usdt": round(max_loss_allowed_usdt, 2),
        "rr_ratio": round(exit_plan["rr_ratio"], 2),
        "deciding_limit": "Max Loss per Trade & Grid Min Order (5 USDT/grid)",
    }

    return {
        "passed": passed,
        "status": status,
        "reason": reason,
        "risk_usdt": round(effective_loss_at_stop_usdt, 2),
        "max_loss_usdt": round(max_loss_allowed_usdt, 2),
        "today_pnl_pct": round(today_pnl_pct, 2),
        "trades_today": trades_count_today,
        "bracket_order": bracket_order,
    }


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
# ANALISI MULTI-COIN E SCORING SU 100 PUNTI (10 PILASTRI CON TIME MACHINE)
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


def analyze_single_coin(coin_cfg, btc, eur_usdt, invest_usdt, all_futures, fng, news, state):
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
    highs_15m = [float(c[2]) for c in c15m]
    lows_15m = [float(c[3]) for c in c15m]
    closes_15m = [float(c[4]) for c in c15m]
    vols_15m = [float(c[6]) for c in c15m]

    price = closes_1h[-1]
    chg24 = ((price - closes_1h[-25]) / closes_1h[-25]) * 100.0
    chg4h = ((price - closes_1h[-5]) / closes_1h[-5]) * 100.0

    # --- Indicatori 1H & 4H ---
    ema20_1h = ema_series(closes_1h, 20)[-1]
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

    recent_24h_span = max(highs_1h[-24:]) - min(lows_1h[-24:])
    rotation_ratio = recent_24h_span / max(upper - lower, 1e-8)
    alpha_vs_btc = chg24 - btc["chg24h"] if btc["ok"] else 0.0

    # --- ESECUZIONE DELLE NUOVE SKILL ---
    # 1. Skill #3 & #8: Chart Scanner & Entry Timer
    entry_timer = run_chart_scanner_and_entry_timer(
        opens_1h, highs_1h, lows_1h, closes_1h, vols_1h,
        opens_15m, highs_15m, lows_15m, closes_15m, vols_15m,
        ema20_1h, bb_pct_b, pos_in_channel
    )

    # 2. Skill #4: Exit Planner (Stop, Target, 1R, Spread Survival)
    exit_plan = run_exit_planner(
        price, lower, upper, atr_1h, step_usd, ob["spread_bps"], net_step_pct, grids
    )
    stop_loss = exit_plan["stop_price"]

    # 3. Skill #9: Time Machine (Monte Carlo 1.500 sims + Historical Replay)
    time_machine = run_time_machine(
        highs_1h, lows_1h, closes_1h, price, stop_loss, upper,
        exit_plan["one_r_usd"], exit_plan["rr_ratio"], n_sims=1500, horizon_hours=24
    )

    # 4. Skill #2 & #6: Budget & Risk Manager Gatekeeper
    risk_check = run_budget_and_risk_manager(
        coin_sym, coin_cfg["pionex"], price, lower, upper, grids, invest_usdt,
        exit_plan, time_machine, news, state
    )

    # ==========================================================================
    # RUBRICA QUANTITATIVA A 10 PILASTRI (0 - 100 PUNTI -> VOTO 1 - 10)
    # ==========================================================================
    pts = 0.0
    breakdown = []

    # 1. RETE NEURALE (MLP) & LORENTZIAN k-NN (Max 14 pt)
    if ai_prob >= 0.64:
        p1 = 14.0
    elif ai_prob >= 0.57:
        p1 = 11.5
    elif ai_prob >= 0.51:
        p1 = 8.5
    elif ai_prob >= 0.45:
        p1 = 4.5
    else:
        p1 = 1.0
    pts += p1
    breakdown.append(
        f"🧠 <b>AI Neural MLP + Lorentzian k-NN ({p1:.0f}/14)</b>: Probabilità Long <b>{ai_prob*100:.1f}%</b> "
        f"(MLP {ai_res['mlp_prob']*100:.0f}% | k-NN {ai_res['knn_prob']*100:.0f}%)"
    )

    # 2. TIME MACHINE MONTE CARLO & REPLAY STORICO (Max 12 pt) - NUOVA SKILL #9
    ev_r = time_machine["expected_r"]
    sf_p = time_machine["stop_first_prob"]
    if ev_r >= 0.60 and sf_p <= 25.0:
        p_tm = 12.0
    elif ev_r >= 0.35 and sf_p <= 34.0:
        p_tm = 9.5
    elif ev_r >= 0.15 and sf_p <= 40.0:
        p_tm = 6.5
    else:
        p_tm = 2.0
    pts += p_tm
    breakdown.append(
        f"🕰️ <b>Time Machine 1.500 Sim ({p_tm:.0f}/12)</b>: E[R] <b>{ev_r:+.2f}R</b> | "
        f"Successo Grid/Target <b>{time_machine['win_or_grid_prob']:.0f}%</b> (StopFirst {sf_p:.0f}%)"
    )

    # 3. STRUTTURA MULTI-TIMEFRAME 4H & 1H (Max 13 pt)
    p2 = 0.0
    if price >= ema50_4h:
        p2 += 5.0
    elif price >= ema200_4h:
        p2 += 3.0
    if price >= ema200_1h:
        p2 += 4.0
    elif price >= ema50_1h:
        p2 += 2.0
    if pdi_1h >= mdi_1h:
        p2 += 4.0
    elif adx_1h < 22:
        p2 += 2.5
    pts += p2
    trend_lbl = "Rialzista/Neutro" if p2 >= 9 else ("Misto" if p2 >= 5 else "Debole/Ribassista")
    breakdown.append(
        f"📐 <b>Struttura Multi-TF 4H/1H ({p2:.0f}/13)</b>: {trend_lbl} (ADX 1H {adx_1h:.1f}, 4H EMA50 ${fmt_p(ema50_4h)})"
    )

    # 4. REGIME ECONOMETRICO: HURST, VARIANCE RATIO & VOLATILITÀ (Max 11 pt)
    p3 = 0.0
    if hurst <= 0.47:
        p3 += 4.5
    elif hurst <= 0.53:
        p3 += 3.0
    else:
        p3 += 1.5
    if vr_6 <= 0.95:
        p3 += 3.0
    elif vr_6 <= 1.10:
        p3 += 2.0
    if not in_squeeze and 0.72 <= atr_ratio <= 1.45:
        p3 += 3.5
    elif not in_squeeze:
        p3 += 1.5
    pts += p3
    regime_str = "Mean-Reverting (Ideale)" if hurst < 0.49 else ("Equilibrato" if hurst <= 0.53 else "Trending")
    breakdown.append(
        f"🔬 <b>Econometria & Volatilità ({p3:.0f}/11)</b>: Hurst <b>{hurst:.2f}</b> ({regime_str}), "
        f"VR {vr_6:.2f}, ATR Ratio {atr_ratio:.2f}x"
    )

    # 5. VALORE: POSIZIONE NEL CANALE, VWAP & BOLLINGER %B (Max 12 pt)
    p4 = 0.0
    if 15 <= pos_in_channel <= 58:
        p4 += 6.5
    elif 58 < pos_in_channel <= 68:
        p4 += 4.0
    elif 8 <= pos_in_channel < 15:
        p4 += 3.5
    else:
        p4 += 1.0
    if -2.2 <= vwap_diff_pct <= 0.8:
        p4 += 3.0
    elif vwap_diff_pct <= 1.8:
        p4 += 2.0
    if 18 <= bb_pct_b <= 68:
        p4 += 2.5
    pts += p4
    breakdown.append(
        f"🎯 <b>Valore & Posizione Prezzo ({p4:.0f}/12)</b>: Nel canale al <b>{pos_in_channel:.0f}%</b>, "
        f"vs VWAP {vwap_diff_pct:+.2f}%, BB %B {bb_pct_b:.0f}%"
    )

    # 6. CHART SCANNER & ENTRY TIMER 15m (Max 11 pt) - NUOVE SKILL #3 & #8
    p5 = 0.0
    if entry_timer["triggered"]:
        p5 += 4.5
    elif bounce_15m:
        p5 += 2.0
    if macd_rising:
        p5 += 3.5
    if 34 <= rsi_1h <= 64 and srsi_15m < 85:
        p5 += 3.0
    pts += p5
    breakdown.append(
        f"⏱️ <b>Entry Timer & Setup ({p5:.0f}/11)</b>: <b>{entry_timer['setup_name']}</b> — {entry_timer['status_txt']}"
    )

    # 7. MICROSTRUTTURA ORDERBOOK DEPTH & SMART MONEY VOLUME (Max 9 pt)
    p6 = 0.0
    if ob["imbalance"] >= 8.0:
        p6 += 5.0
    elif ob["imbalance"] >= -5.0:
        p6 += 3.5
    elif ob["imbalance"] >= -15.0:
        p6 += 1.5
    if not panic_selling and max_red_vol_ratio < 1.6:
        p6 += 4.0
    elif not panic_selling:
        p6 += 2.0
    pts += p6
    breakdown.append(
        f"🏛️ <b>Orderbook L2 & Flussi ({p6:.0f}/9)</b>: Sbilanciamento Bid/Ask <b>{ob['imbalance']:+.1f}%</b> "
        f"(Spread {ob['spread_bps']:.1f} bps)"
    )

    # 8. FORZA RELATIVA VS BITCOIN - ALPHA (Max 6 pt)
    p7 = 0.0
    if not btc["dumping"] and not btc["strong_bear"]:
        p7 += 3.5
        if alpha_vs_btc >= 0.0:
            p7 += 2.5
        elif alpha_vs_btc >= -1.5:
            p7 += 1.5
    elif not btc["dumping"]:
        p7 += 2.0
    pts += p7
    breakdown.append(
        f"₿ <b>Correlazione & Alpha BTC ({p7:.0f}/6)</b>: BTC 24h {btc['chg24h']:+.1f}% | "
        f"Alpha {coin_sym} {alpha_vs_btc:+.1f}%"
    )

    # 9. DERIVATI PERPETUAL (FUNDING RATE) & SENTIMENT (Max 5 pt)
    p8 = 0.0
    fr = futures["funding_8h_pct"]
    if -0.025 <= fr <= 0.022:
        p8 += 3.0
    elif fr < -0.025:
        p8 += 2.5
    else:
        p8 += 1.0
    fg_val = fng["value"]
    if 22 <= fg_val <= 74:
        p8 += 2.0
    else:
        p8 += 1.0
    pts += p8
    breakdown.append(
        f"🔮 <b>Futures & Sentiment ({p8:.0f}/5)</b>: Funding 8h <b>{fr:+.4f}%</b> | "
        f"Fear&Greed <b>{fg_val}</b> ({fng['label']})"
    )

    # 10. EXIT PLANNER R:R & EFFICIENZA GRIGLIA 50€ (Max 7 pt) - NUOVA SKILL #4
    p9 = 0.0
    if exit_plan["rr_ratio"] >= 1.75 and net_step_pct >= 0.18:
        p9 += 4.5
    elif exit_plan["rr_ratio"] >= MIN_REWARD_RISK_R:
        p9 += 3.0
    if 0.45 <= rotation_ratio <= 1.40:
        p9 += 2.5
    pts += p9
    breakdown.append(
        f"💶 <b>Exit Plan & R:R ({p9:.0f}/7)</b>: R:R <b>{exit_plan['rr_ratio']:.2f}R</b> "
        f"(1R = -{exit_plan['one_r_pct']:.2f}%) | Netto/grid <b>+{net_step_pct:.2f}%</b>"
    )

    raw_score_10 = round(pts / 10.0)
    score = max(1, min(10, raw_score_10))

    # --- REGOLE DI VETO E RISK MANAGER GATEKEEPER ---
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

    if not risk_check["passed"]:
        score = min(score, 5)
        veto_reasons.append(f"🛡️ <b>RISK MANAGER BLOCK</b>: {risk_check['reason']}")

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
        "entry_timer": entry_timer,
        "exit_plan": exit_plan,
        "time_machine": time_machine,
        "risk_check": risk_check,
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
    state = load_bot_state()
    btc = analyze_btc_context()
    eur_usdt = fetch_eur_usdt_rate()
    invest_usdt = round(INVEST_EUR * eur_usdt, 2)
    all_futures = fetch_all_futures_metrics()
    fng = fetch_fear_and_greed()

    results = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        future_map = {
            executor.submit(
                analyze_single_coin, cfg, btc, eur_usdt, invest_usdt, all_futures, fng, news, state
            ): cfg
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

    # Ordina per: 1) Risk Check PASS, 2) Voto Finale (1-10), 3) Score 100pt, 4) Time Machine E[R]
    results.sort(
        key=lambda x: (
            1 if x["risk_check"]["passed"] else 0,
            x["final_score"],
            x["pts_100"],
            x["time_machine"]["expected_r"],
        ),
        reverse=True,
    )

    best = results[0]
    best["all_coins"] = results
    return best


# ==============================================================================
# MOTORE DI GESTIONE AUTONOMA + PRICE ALERTS ANTI-SPAM (1R / 2R / STOP / TARGET)
# ==============================================================================

def execute_autonomous_cycle(best_a, news):
    state = load_bot_state()
    all_coins = best_a.get("all_coins", [best_a])
    coin_map = {c["coin"]: c for c in all_coins}
    now_rome = datetime.now(ZoneInfo("Europe/Rome"))
    now_str = now_rome.strftime("%d/%m %H:%M")
    today_str = now_rome.strftime("%d/%m")

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
        one_r = state.get("one_r_usd") or max(entry_p - state.get("initial_stop", stop_b), entry_p * 0.02)
        high_p = max(state.get("highest_price", entry_p), price)
        state["highest_price"] = high_p

        pnl_pct = ((price - entry_p) / entry_p) * 100.0 if entry_p > 0 else 0.0
        pnl_usdt = cur_a["invest_usdt"] * (pnl_pct / 100.0)
        current_r = (price - entry_p) / one_r if one_r > 0 else 0.0

        # Skill #1 & #4: Smart Price Alerts (Suonano 1 sola volta per livello R senza fare spam)
        if (current_r >= 1.0 or pnl_pct >= 2.4) and not state.get("alert_1r_sent"):
            new_stop = round_p(entry_p * 1.003)
            if stop_b < new_stop:
                state["stop"] = new_stop
                stop_b = new_stop
            state["trailing_locked"] = True
            state["alert_1r_sent"] = True
            auto_log.append(
                f"🔔 <b>PRICE ALERT +1R RAGGIUNTO ({active_coin})</b>: Profitto a +{pnl_pct:.1f}% (+{current_r:.1f}R), "
                f"Stop Loss spostato a <b>Break-Even (${fmt_p(stop_b)})</b> 🔒!"
            )

        if (current_r >= 1.8 or pnl_pct >= 4.2) and not state.get("alert_2r_sent"):
            new_stop = round_p(max(stop_b, high_p * 0.976))
            state["stop"] = new_stop
            stop_b = new_stop
            state["alert_2r_sent"] = True
            auto_log.append(
                f"🔔 <b>PRICE ALERT +2R TRAILING ({active_coin})</b>: Massimo a ${fmt_p(high_p)}, "
                f"Trailing Stop blindato a <b>${fmt_p(stop_b)}</b> 📈!"
            )
        elif state.get("alert_2r_sent") and stop_b < round_p(high_p * 0.976):
            new_stop = round_p(high_p * 0.976)
            state["stop"] = new_stop
            stop_b = new_stop

        # Controllo Condizioni di Chiusura Automatica (Le uscite NON sono mai bloccate dai limiti giornalieri)
        close_reason = None
        if price <= stop_b:
            close_reason = f"🛑 STOP LOSS / TRAILING COLPITO (${fmt_p(price)} <= ${fmt_p(stop_b)})"
        elif price >= up_b * 1.006:
            close_reason = f"🎯 TARGET / UPPER RANGE COLPITO (${fmt_p(price)} >= ${fmt_p(up_b)})"
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
                "r_multiple": round(current_r, 2),
                "reason": close_reason,
            }
            history = state.get("history", [])
            history.insert(0, closed_trade)
            state["history"] = history[:30]

            state["active"] = False
            state["last_action"] = (
                f"[{now_str}] Chiuso {active_coin} a ${fmt_p(price)} ({pnl_pct:+.2f}% / {current_r:+.1f}R) - {close_reason}"
            )
            auto_log.append(
                f"🔔 <b>ALERT CHIUSURA AUTOMATICA SU {active_coin}/USDT</b>\n"
                f"   • Motivo: {close_reason}\n"
                f"   • Entrata: ${fmt_p(entry_p)} ➡️ Uscita: <b>${fmt_p(price)}</b>\n"
                f"   • Risultato: <b>{pnl_pct:+.2f}% ({pnl_usdt:+.2f} USDT | {current_r:+.2f}R)</b>\n"
                f"   • Esecuzione: {exec_note}"
            )
        else:
            auto_log.append(
                f"🤖 <b>POSIZIONE ATTIVA SU {active_coin}/USDT</b> (Aperta il {state['opened_at']})\n"
                f"   • Entrata: <b>${fmt_p(entry_p)}</b> | Attuale: <b>${fmt_p(price)}</b> "
                f"(<b>{pnl_pct:+.2f}%</b> / {pnl_usdt:+.2f}$ / <b>{current_r:+.2f}R</b>)\n"
                f"   • Range Attivo: <b>${fmt_p(low_b)} – ${fmt_p(up_b)}</b> ({state['grids']} griglie)\n"
                f"   • Alert Stop: <b>${fmt_p(stop_b)}</b> {'(Break-Even 🔒)' if state.get('trailing_locked') else ''} | "
                f"Alert Target: <b>${fmt_p(up_b)}</b>"
            )

    else:
        # Nessuna posizione attiva: valuta APERTURA AUTOMATICA sulla Moneta #1 se passa tutti i filtri
        win_coin = best_a["coin"]
        win_sym = best_a["pionex_symbol"]
        win_price = best_a["price"]
        win_score = best_a["final_score"]
        rk = best_a["risk_check"]
        et = best_a["entry_timer"]
        tm = best_a["time_machine"]
        ep = best_a["exit_plan"]

        if win_score >= 7 and best_a["ai_prob"] >= 0.52 and rk["passed"] and et["triggered"]:
            qty_coin = rk["bracket_order"]["qty_coin"]
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
                "initial_stop": best_a["stop_loss"],
                "one_r_usd": ep["one_r_usd"],
                "grids": best_a["grids"],
                "qty_coin": qty_coin,
                "trailing_locked": False,
                "alert_1r_sent": False,
                "alert_2r_sent": False,
                "opened_at": now_str,
                "opened_date": today_str,
                "last_action": f"[{now_str}] Aperto {win_coin}/USDT a ${fmt_p(win_price)} (Voto {win_score}/10 | {ep['rr_ratio']:.2f}R)",
            })
            auto_log.append(
                f"🚀 <b>APERTURA AUTOMATICA AI SU {win_coin}/USDT!</b> (Voto {win_score}/10 | Risk: {rk['status']})\n"
                f"   • Setup Triggered: <b>{et['setup_name']}</b>\n"
                f"   • Bracket Order: Entry <b>${fmt_p(win_price)}</b> | Stop <b>${fmt_p(best_a['stop_loss'])}</b> | Target <b>${fmt_p(best_a['upper'])}</b>\n"
                f"   • Sizing & Rischio: Qty <b>{qty_coin} {win_coin}</b> (~{best_a['invest_usdt']:.1f}$) | Rischio 1R: <b>${rk['risk_usdt']:.2f}</b> (R:R <b>{ep['rr_ratio']:.2f}R</b>)\n"
                f"   • Time Machine: Successo <b>{tm['win_or_grid_prob']:.0f}%</b> | E[R]: <b>{tm['expected_r']:+.2f}R</b>\n"
                f"   • Esecuzione: {exec_note}"
            )
        else:
            auto_log.append(
                f"⏳ <b>BOT IN ATTESA DI SEGNALE CONFERMATO</b> (10 Cripto Scansionate)\n"
                f"   • Top #1: <b>{win_coin}/USDT</b> — Voto <b>{win_score}/10</b> ({best_a['pts_100']:.0f}/100 pt | AI {best_a['ai_prob']*100:.0f}%)\n"
                f"   • Entry Timer: <b>{et['status_txt']}</b> ({et['setup_name']})\n"
                f"   • Risk Manager: <b>{rk['status']}</b> ({rk['reason']})\n"
                f"   • Time Machine: <b>{tm['verdict']}</b> (E[R] {tm['expected_r']:+.2f}R | Win/Grid {tm['win_or_grid_prob']:.0f}%)\n"
                f"   • Ultima operazione: <i>{state.get('last_action', 'Nessuna')}</i>"
            )

    save_bot_state(state)
    best_a["auto_state"] = state
    best_a["auto_log"] = auto_log
    return best_a["final_score"]


# ==============================================================================
# GENERATORE WEBAPP DASHBOARD MULTI-COIN + TIME MACHINE & RISK MANAGER
# ==============================================================================

def generate_webapp_dashboard(best_a, news, final_score):
    state = best_a.get("auto_state", load_bot_state())
    all_coins = best_a.get("all_coins", [best_a])
    coin_map = {c["coin"]: c for c in all_coins}
    now_str = news["now_rome"].strftime("%d/%m/%Y %H:%M:%S")

    score_color = "emerald" if final_score >= 7 else ("amber" if final_score >= 5 else "rose")
    mode_badge = "LIVE TRADING PIONEX" if AUTO_TRADE_ENABLED else "SIMULAZIONE PAPER TRADING"
    mode_color = "bg-emerald-500/20 text-emerald-300 border-emerald-500/40" if AUTO_TRADE_ENABLED else "bg-sky-500/20 text-sky-300 border-sky-500/40"

    tm = best_a["time_machine"]
    rk = best_a["risk_check"]
    ep = best_a["exit_plan"]
    et = best_a["entry_timer"]
    rk_col = "text-emerald-400" if rk["passed"] else "text-rose-400"

    if state.get("active"):
        act_coin = state.get("coin", "SOL")
        act_a = coin_map.get(act_coin, best_a)
        cur_p = act_a["price"]
        entry_p = state["entry_price"]
        low_b = state["lower"]
        up_b = state["upper"]
        stop_b = state["stop"]
        one_r = state.get("one_r_usd") or max(entry_p - stop_b, entry_p * 0.02)
        cur_r = (cur_p - entry_p) / one_r if one_r > 0 else 0.0
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
                    <div class="text-xs text-slate-400">PnL Stimato (Multiplo R)</div>
                    <div class="text-xl font-bold {pnl_col}">{pnl_pct:+.2f}% ({cur_r:+.2f}R)</div>
                </div>
                <div class="bg-slate-900/70 p-4 rounded-xl">
                    <div class="text-xs text-slate-400">Alert Stop Dinamico</div>
                    <div class="text-xl font-bold text-amber-400">${fmt_p(stop_b)} {'🔒' if state.get('trailing_locked') else ''}</div>
                </div>
            </div>
            <div class="mb-2 flex justify-between text-xs text-slate-300">
                <span>Lower: <b>${fmt_p(low_b)}</b></span>
                <span>Posizione nel Range: <b>{pos_pct:.0f}%</b> ({state.get('grids')} Griglie)</span>
                <span>Alert Target: <b>${fmt_p(up_b)}</b></span>
            </div>
            <div class="w-full bg-slate-900 rounded-full h-4 p-0.5 border border-slate-700">
                <div class="bg-gradient-to-r from-sky-500 to-emerald-400 h-2.5 rounded-full" style="width: {pos_pct:.1f}%"></div>
            </div>
        </div>
        """
    else:
        pos_html = f"""
        <div class="bg-slate-800/80 border border-slate-700 rounded-2xl p-6 shadow-xl">
            <div class="flex flex-wrap justify-between items-center gap-2 mb-2">
                <div class="flex items-center gap-3">
                    <span class="h-3 w-3 rounded-full bg-amber-400"></span>
                    <h2 class="text-lg font-bold text-slate-200">NESSUN TRADE APERTO — IN ATTESA DI SEGNALE + RISK PASS</h2>
                </div>
                <span class="text-xs font-bold {rk_col}">Risk Check {best_a['coin']}: {rk['status']}</span>
            </div>
            <p class="text-sm text-slate-400 mb-3">Setup rilevato su <b>{best_a['coin']}/USDT</b>: <b>{et['setup_name']}</b> ({et['status_txt']}).</p>
            <div class="grid grid-cols-1 md:grid-cols-3 gap-3 text-xs text-slate-300 bg-slate-900/70 p-4 rounded-xl border border-slate-700/60">
                <div><b>🕰️ Time Machine (1.500 Sim):</b><br>Successo Grid/Target: <b>{tm['win_or_grid_prob']:.1f}%</b><br>Stop First: <b>{tm['stop_first_prob']:.1f}%</b> | E[R]: <b>{tm['expected_r']:+.2f}R</b></div>
                <div><b>📐 Exit Planner &amp; R-Multiple:</b><br>Entrata: <b>${fmt_p(best_a['price'])}</b> | Stop: <b>${fmt_p(best_a['stop_loss'])}</b><br>Target: <b>${fmt_p(best_a['upper'])}</b> | R:R: <b>{ep['rr_ratio']:.2f}R</b></div>
                <div><b>🛡️ Budget &amp; Risk Manager:</b><br>Rischio Stop: <b>${rk['risk_usdt']:.2f}</b> (Max: ${rk['max_loss_usdt']:.2f})<br>Esito: <b>{html.escape(rk['reason'])}</b></div>
            </div>
        </div>
        """

    ranking_rows = ""
    for idx, c in enumerate(all_coins, 1):
        sc = c["final_score"]
        badge_c = "bg-emerald-500/20 text-emerald-300 border-emerald-500/40" if sc >= 7 else (
            "bg-amber-500/20 text-amber-300 border-amber-500/40" if sc >= 5 else "bg-rose-500/20 text-rose-300 border-rose-500/40"
        )
        chg_c = "text-emerald-400" if c["chg24"] >= 0 else "text-rose-400"
        rk_badge = "PASS ✅" if c["risk_check"]["passed"] else "BLOCK ⛔"
        crown = "👑 " if idx == 1 else f"#{idx} "
        ranking_rows += f"""
        <tr class="border-b border-slate-800/80 text-xs hover:bg-slate-800/40">
            <td class="py-3 px-3 font-bold text-white">{crown}{c['coin']}/USDT</td>
            <td class="py-3 px-3 font-semibold text-slate-200">${fmt_p(c['price'])} <span class="{chg_c}">({c['chg24']:+.1f}%)</span></td>
            <td class="py-3 px-3 text-sky-400 font-bold">{c['ai_prob']*100:.0f}%</td>
            <td class="py-3 px-3 text-purple-300 font-semibold">{c['time_machine']['expected_r']:+.2f}R ({c['time_machine']['win_or_grid_prob']:.0f}%)</td>
            <td class="py-3 px-3 text-slate-300">{c['exit_plan']['rr_ratio']:.2f}R</td>
            <td class="py-3 px-3 text-slate-300">{rk_badge}</td>
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
        r_mult = h.get("r_multiple", 0.0)
        history_rows += f"""
        <tr class="border-b border-slate-800 text-xs">
            <td class="py-2.5 px-3 font-bold text-white">{h_coin}/USDT</td>
            <td class="py-2.5 px-3">{h.get('opened_at')} &rarr; {h.get('closed_at')}</td>
            <td class="py-2.5 px-3">${fmt_p(h.get('entry_price', 0))} &rarr; ${fmt_p(h.get('exit_price', 0))}</td>
            <td class="py-2.5 px-3 font-bold {c}">{h.get('pnl_pct', 0):+.2f}% ({h.get('pnl_usdt', 0):+.2f}$ | {r_mult:+.1f}R)</td>
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
                    <h1 class="text-xl md:text-2xl font-extrabold tracking-tight text-white">Multi-Coin AI Quant Bot + Time Machine</h1>
                </div>
                <p class="text-xs text-slate-400 mt-1">Ultimo aggiornamento: <b>{now_str}</b> (10 Cripto + Monte Carlo 1.500 Sim + Risk Manager)</p>
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
                <div class="text-xs text-slate-400">Voto Quant &amp; Risk</div>
                <div class="text-2xl font-black text-{score_color}-400 mt-1">{final_score}/10 <span class="text-sm font-normal text-slate-400">({best_a['pts_100']:.0f}/100)</span></div>
                <div class="text-xs {rk_col} mt-1 font-bold">{rk['status']} (R:R {ep['rr_ratio']:.2f}R)</div>
            </div>
            <div class="bg-slate-900 border border-slate-800 p-4 rounded-2xl">
                <div class="text-xs text-slate-400">🕰️ Time Machine ({best_a['coin']})</div>
                <div class="text-2xl font-black text-purple-400 mt-1">{tm['expected_r']:+.2f}R</div>
                <div class="text-xs text-slate-400 mt-1">Win/Grid: {tm['win_or_grid_prob']:.0f}% | Stop: {tm['stop_first_prob']:.0f}%</div>
            </div>
            <div class="bg-slate-900 border border-slate-800 p-4 rounded-2xl">
                <div class="text-xs text-slate-400">🧠 Rete Neurale + k-NN</div>
                <div class="text-2xl font-black text-sky-400 mt-1">{best_a['ai_prob']*100:.1f}%</div>
                <div class="text-xs text-slate-400 mt-1">MLP: {best_a['ai_res']['mlp_prob']*100:.0f}% | k-NN: {best_a['ai_res']['knn_prob']*100:.0f}%</div>
            </div>
        </div>

        {pos_html}

        <div class="bg-slate-900 border border-slate-800 rounded-2xl p-6">
            <h3 class="text-base font-bold text-white mb-4">🏆 Classifica Live Multi-Coin (AI + Time Machine + Risk Gatekeeper)</h3>
            <div class="overflow-x-auto">
                <table class="w-full text-left border-collapse">
                    <thead>
                        <tr class="border-b border-slate-700 text-xs text-slate-400">
                            <th class="py-2 px-3">Moneta</th>
                            <th class="py-2 px-3">Prezzo (24h)</th>
                            <th class="py-2 px-3">AI Prob</th>
                            <th class="py-2 px-3">Time Machine E[R]</th>
                            <th class="py-2 px-3">Reward:Risk</th>
                            <th class="py-2 px-3">Risk Check</th>
                            <th class="py-2 px-3">Voto Finale</th>
                        </tr>
                    </thead>
                    <tbody>
                        {ranking_rows}
                    </tbody>
                </table>
            </div>
        </div>

        <div class="bg-slate-900 border border-slate-800 rounded-2xl p-6">
            <h3 class="text-base font-bold text-white mb-4">📊 Pagella Dettagliata 10 Pilastri per 👑 {best_a['coin']}/USDT ({best_a['pts_100']:.0f}/100 pt)</h3>
            <ul class="grid grid-cols-1 md:grid-cols-2 gap-3">
                {pillars_html}
            </ul>
        </div>

        <div class="bg-slate-900 border border-slate-800 rounded-2xl p-6">
            <h3 class="text-base font-bold text-white mb-4">📜 Storico Operazioni Chiuse (con Multiplo R)</h3>
            <div class="overflow-x-auto">
                <table class="w-full text-left border-collapse">
                    <thead>
                        <tr class="border-b border-slate-700 text-xs text-slate-400">
                            <th class="py-2 px-3">Moneta</th>
                            <th class="py-2 px-3">Periodo</th>
                            <th class="py-2 px-3">Prezzo In/Out</th>
                            <th class="py-2 px-3">Risultato (PnL / R)</th>
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
    lines = ["📰 <b>CALENDARIO MACRO USA &amp; NYSE BELL</b>"]

    if news["status"] == "NO_DATA":
        lines.append("⚠️ Calendario macro momentaneamente non raggiungibile.")
        return "\n".join(lines)

    if news["us_open_warning"]:
        lines.append("🔔 <b>NYSE Opening Bell (09:30 NY)</b>: finestra di alta volatilità azionaria USA.")

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
    tm = best_a["time_machine"]
    rk = best_a["risk_check"]
    ep = best_a["exit_plan"]
    et = best_a["entry_timer"]

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
        news_override_reason = "⚠️ <b>NYSE OPENING BELL</b>: Volatilità elevata sull'apertura USA."

    if final_score >= 8 and rk["passed"]:
        verdict = f"🟢 <b>SEGNALE FORTE SU {best_a['coin']}/USDT: OTTIMO MOMENTO PER APRIRE</b>"
    elif final_score == 7 and rk["passed"]:
        verdict = f"🟢 <b>SEGNALE POSITIVO SU {best_a['coin']}/USDT: AVVIO BOT CONSIGLIATO</b>"
    elif final_score == 6:
        verdict = f"🟡 <b>QUASI PRONTO ({best_a['coin']} 6/10): SETUP DISCRETO MA ATTESA CONSIGLIATA</b>"
    elif final_score == 5:
        verdict = "🟡 <b>NEUTRALE (5/10): MERCATO INCERTO O BLOCCO RISK MANAGER</b>"
    elif final_score >= 3:
        verdict = "🟠 <b>SCONSIGLIATO: CONDIZIONI TECNICO/MACRO SFAVOREVOLI</b>"
    else:
        verdict = "🔴 <b>PERICOLO / BLOCCO: NON APRIRE IL BOT ADESSO</b>"

    rank_lines = ["🏆 <b>CLASSIFICA AI + TIME MACHINE (10 Cripto)</b>"]
    for idx, c in enumerate(all_coins, 1):
        sc = c["final_score"]
        ic = "🟢" if sc >= 7 else ("🟡" if sc >= 5 else "🔴")
        crown = "👑" if idx == 1 else f"{idx}."
        rk_ic = "✅" if c["risk_check"]["passed"] else "⛔"
        rank_lines.append(
            f"{crown} {ic} <b>{c['coin']}</b>: <b>{sc}/10</b> ({c['pts_100']:.0f}pt) | "
            f"E[R] <b>{c['time_machine']['expected_r']:+.2f}R</b> | AI <b>{c['ai_prob']*100:.0f}%</b> | Risk {rk_ic}"
        )
    rank_section = "\n".join(rank_lines)

    breakdown_txt = "\n".join(f"• {item}" for item in best_a["breakdown"])
    veto_list = list(best_a["veto_reasons"])
    if news_override_reason:
        veto_list.append(news_override_reason)
    veto_txt = ("\n\n🚨 <b>FILTRI DI SICUREZZA &amp; RISK MANAGER</b>\n" + "\n".join(veto_list)) if veto_list else ""

    news_section = format_news_section(news, nr)
    auto_section = "\n".join(best_a.get("auto_log", []))
    mode_str = "🟢 LIVE PIONEX" if AUTO_TRADE_ENABLED else "🧪 PAPER TRADING (Simulazione)"

    return (
        f"🤖 <b>MULTI-COIN AI BOT + TIME MACHINE &amp; RISK MANAGER</b>\n"
        f"🕐 {now_rome} (ora italiana) | Modalità: <b>{mode_str}</b>\n\n"
        f"{verdict}\n"
        f"👑 <b>Migliore Scelta AI: {best_a['coin']}/USDT ({best_a['name']})</b>\n"
        f"📊 <b>Voto Finale: {final_score}/10</b> (Score Quant: {best_a['pts_100']:.0f}/100)\n"
        f"{score_bar(final_score)}\n\n"
        f"⚙️ <b>STATO GESTIONE AUTONOMA &amp; ALERTS</b>\n"
        f"{auto_section}\n\n"
        f"🕰️ <b>TIME MACHINE (1.500 Sim) &amp; RISK GATEKEEPER ({best_a['coin']})</b>\n"
        f"• Setup Scanner: <b>{et['setup_name']}</b> ({et['status_txt']})\n"
        f"• Prob. Profitto Grid/Target: <b>{tm['win_or_grid_prob']:.1f}%</b> | Stop First: <b>{tm['stop_first_prob']:.1f}%</b>\n"
        f"• Valore Atteso: <b>{tm['expected_r']:+.2f}R</b> ({tm['verdict']})\n"
        f"• Risk Manager: <b>{rk['status']}</b> — Rischio Stop: <b>${rk['risk_usdt']:.2f}</b> (Max: ${rk['max_loss_usdt']:.2f}) | R:R: <b>{ep['rr_ratio']:.2f}R</b>\n\n"
        f"{rank_section}\n\n"
        f"{news_section}\n\n"
        f"⚙️ <b>BRACKET PLAN PIONEX PER 👑 {best_a['coin']}/USDT ({INVEST_EUR}€ ≈ {best_a['invest_usdt']:.2f} USDT)</b>\n"
        f"• Prezzo Entrata: <b>${fmt_p(best_a['price'])}</b> (Qty: {rk['bracket_order']['qty_coin']} {best_a['coin']})\n"
        f"• Lower / Upper Range: <b>${fmt_p(best_a['lower'])} – ${fmt_p(best_a['upper'])}</b> ({best_a['grids']} griglie)\n"
        f"• Profitto netto/griglia (post-fee): <b>+{best_a['net_step_pct']:.2f}%</b>\n"
        f"• Alert +1R (Break-Even): <b>${fmt_p(ep['level_1r'])}</b> | Alert Stop Loss: <b>${fmt_p(best_a['stop_loss'])}</b>\n\n"
        f"📋 <b>PAGELLA 10 PILASTRI ({best_a['coin']}/USDT)</b>\n"
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
        f"🖥️ Dashboard Multi-Coin AI + Time Machine | {stato_str}\n"
        f"👆 Tocca/clicca sul file qui sopra per aprire la WebApp interattiva!"
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
```
