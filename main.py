"""
Pionex Fast-Scalp AI Engine v8.0
- Esecuzione ogni 30 min nelle ore migliori (vedi workflow), max 5 trade/giorno.
- Modalita' LIVE solo se: chiavi Pionex presenti E AUTO_TRADE_ENABLED=true (altrimenti SIMULAZIONE).
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
# CONFIGURAZIONE
# ==============================================================================
def _env(name, default):
    v = os.getenv(name)
    return v.strip() if v is not None and v.strip() != "" else str(default)


TELEGRAM_TOKEN = (
    os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("BOT_TOKEN") or os.getenv("TOKEN") or ""
).strip()
TELEGRAM_CHAT_ID = (
    os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or os.getenv("TELEGRAM_CHAT") or ""
).strip()
PIONEX_API_KEY = (os.getenv("PIONEX_API_KEY") or os.getenv("API_KEY") or "").strip()
PIONEX_API_SECRET = (os.getenv("PIONEX_API_SECRET") or os.getenv("API_SECRET") or "").strip()
GITHUB_EVENT_NAME = _env("GITHUB_EVENT_NAME", "manual")
# Comandi manuali (da console HTML / workflow_dispatch): status | close | sell_all | open
MANUAL_COMMAND = _env("MANUAL_COMMAND", "").lower()
MANUAL_COIN = _env("MANUAL_COIN", "").upper()
try:
    MANUAL_USDT = float(_env("MANUAL_USDT", 0))
except ValueError:
    MANUAL_USDT = 0.0

INVEST_EUR = float(_env("INVEST_EUR", 50.0))
# SICUREZZA: default FALSE. Il trading reale parte solo con AUTO_TRADE_ENABLED=true esplicito.
AUTO_TRADE_ENABLED = _env("AUTO_TRADE_ENABLED", "false").lower() == "true"

# Ingressi / rischio
Entry_Score_Min = 6
Entry_Raw_100_Min = 58
RISK_PER_TRADE_PCT = float(_env("RISK_PER_TRADE_PCT", 1.5))   # % del capitale rischiato per trade
DAILY_MAX_LOSS_PCT = float(_env("DAILY_MAX_LOSS_PCT", 4.0))   # % del CAPITALE (non somma di % per-trade)
MAX_TRADES_PER_DAY = int(_env("MAX_TRADES_PER_DAY", 5))
MAX_CONSEC_LOSSES = int(_env("MAX_CONSEC_LOSSES", 3))
MIN_REWARD_RISK_R = 1.15          # R:R NETTO di commissioni (~1.25 lordo)
CASH_BUFFER_PCT = 5.0
MIN_ORDER_USDT = float(_env("MIN_ORDER_USDT", 10.5))
FEE_PCT_PER_SIDE = float(_env("FEE_PCT_PER_SIDE", 0.05))      # verifica sul tuo account Pionex
PIONEX_FEE_ROUNDTRIP_PCT = 2.0 * FEE_PCT_PER_SIDE
SCALP_MIN_NET_PROFIT_PCT = 0.65
TIME_STOP_PROFIT_HOURS = 4.5
TIME_STOP_STALE_HOURS = 6.0

# Finestra operativa (UTC): nuovi ingressi solo qui. Copre Londra + Wall Street (ora legale e solare).
ENTRY_START_UTC = int(_env("ENTRY_START_UTC", 7))
ENTRY_END_UTC = int(_env("ENTRY_END_UTC", 21))
MIN_MINUTES_BETWEEN_TRADES = int(_env("MIN_MINUTES_BETWEEN_TRADES", 60))
COOLDOWN_AFTER_LOSS_MIN = int(_env("COOLDOWN_AFTER_LOSS_MIN", 150))
SAME_COIN_COOLDOWN_H = float(_env("SAME_COIN_COOLDOWN_H", 6.0))

STATE_FILE = "bot_state.json"
WEBAPP_FILE = "index.html"
HTTP_TIMEOUT = 12
PIONEX_BASE = "https://api.pionex.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PionexAutonomousQuantBot/8.0)"}

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
LIVE_PX = {}  # prezzi live Pionex (symbol -> last)


# ==============================================================================
# 1. UTILITA' (log con redazione segreti, sessione HTTP con retry, throttle Kraken)
# ==============================================================================
_SECRETS = [s for s in (TELEGRAM_TOKEN, PIONEX_API_KEY, PIONEX_API_SECRET) if s]


def redact(text):
    t = str(text)
    for s in _SECRETS:
        t = t.replace(s, "***")
    return t


def log(level, msg):
    print(f"[{level}] {redact(msg)}", flush=True)


def _build_session():
    s = requests.Session()
    retry = Retry(
        total=2, backoff_factor=0.7, status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]), respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    s.mount("https://", adapter)
    s.headers.update(HEADERS)
    return s


SESSION = _build_session()
_KRAKEN_LOCK = threading.Lock()
_KRAKEN_LAST = [0.0]


def kraken_throttle(min_gap=0.40):
    with _KRAKEN_LOCK:
        wait = _KRAKEN_LAST[0] + min_gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _KRAKEN_LAST[0] = time.monotonic()


def safe_get_json(url, params=None, retries=2):
    is_kraken = "api.kraken.com" in url
    for attempt in range(retries + 1):
        try:
            if is_kraken:
                kraken_throttle()
            r = SESSION.get(url, params=params, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            j = r.json()
            if is_kraken and isinstance(j, dict) and any("rate limit" in str(e).lower() for e in j.get("error", [])):
                time.sleep(2.0 * (attempt + 1))
                continue
            return j
        except Exception as e:
            if attempt < retries:
                time.sleep(0.7 * (attempt + 1))
            else:
                log("WARN", f"JSON fetch error ({url}): {e}")
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


def floor_to_scale(x, scale):
    f = 10 ** int(scale)
    return math.floor(x * f + 1e-9) / f


def fmt_qty(x, scale):
    s = f"{x:.{int(scale)}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


# ==============================================================================
# 2. MEMORIA PERSISTENTE
# ==============================================================================
def load_state():
    default_state = {
        "position": {
            "is_open": False, "coin_code": "SOL", "symbol": "SOL_USDT",
            "entry_price": 0.0, "amount_coin": 0.0, "invested_usdt": 0.0,
            "sl_price": 0.0, "tp_price": 0.0, "initial_sl": 0.0, "one_r": 0.0,
            "highest_price": 0.0, "breakeven_locked": False, "tp1_locked": False,
            "tp2_locked": False, "alerts_triggered": [], "opened_at": "", "opened_ts": 0,
            "last_check_ts": 0, "close_fail_count": 0, "mode": "SIMULATION", "order_id": ""
        },
        "stats": {
            "total_trades": 0, "wins": 0, "losses": 0, "total_pnl_pct": 0.0, "total_pnl_usdt": 0.0,
            "daily_date": "", "daily_pnl_pct": 0.0, "daily_pnl_usdt": 0.0, "daily_trades": 0,
            "consec_losses": 0, "last_close_ts": 0, "last_loss_ts": 0, "coin_last_close": {},
            "last_action": "Nessuna operazione recente"
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
        except Exception as e:
            log("WARN", f"Impossibile leggere {STATE_FILE}: {e}")
    return default_state


def save_state(state):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)  # scrittura atomica
    except Exception as e:
        log("ERROR", f"Salvataggio {STATE_FILE} fallito: {e}")


# ==============================================================================
# 3. CLIENT PIONEX (firma HMAC-SHA256 con query ordinata, saldi, regole simbolo)
# ==============================================================================
def pionex_live_enabled():
    return bool(PIONEX_API_KEY and PIONEX_API_SECRET and AUTO_TRADE_ENABLED)


def pionex_request(method, path, params=None, body=None, timeout=HTTP_TIMEOUT):
    """Richiesta firmata: METHOD + PATH + '?' + query ordinata (con timestamp) + body JSON compatto."""
    params = {k: v for k, v in (params or {}).items() if v is not None}
    params["timestamp"] = int(time.time() * 1000)
    query = urlencode(sorted(params.items()))
    body_str = json.dumps(body, separators=(",", ":")) if body else ""
    message = f"{method}{path}?{query}{body_str}"
    signature = hmac.new(PIONEX_API_SECRET.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    headers = {"PIONEX-KEY": PIONEX_API_KEY, "PIONEX-SIGNATURE": signature, "Content-Type": "application/json"}
    url = f"{PIONEX_BASE}{path}?{query}"
    try:
        r = SESSION.request(method, url, data=body_str if body else None, headers=headers, timeout=timeout)
        try:
            rj = r.json()
        except ValueError:
            return {"ok": False, "msg": f"HTTP {r.status_code} risposta non JSON", "data": None}
    except Exception as e:
        return {"ok": False, "msg": f"Errore connessione Pionex: {redact(e)}", "data": None, "network_error": True}

    # Pionex puo' rispondere HTTP 200 con result=false; alcune risposte hanno 'code' senza 'result'.
    failed = (rj.get("result") is False) or ("result" not in rj and "code" in rj)
    if r.status_code != 200 or failed:
        txt = f"{rj.get('code', '')} {rj.get('message', rj.get('reason', ''))}".strip()
        return {"ok": False, "msg": txt or f"HTTP {r.status_code}", "data": rj.get("data")}
    return {"ok": True, "msg": "OK", "data": rj.get("data")}


def pionex_get_balances():
    r = pionex_request("GET", "/api/v1/account/balances")
    if not r["ok"]:
        log("WARN", f"Lettura saldi Pionex fallita: {r['msg']}")
        return None
    out = {}
    for b in (r["data"] or {}).get("balances", []):
        try:
            out[str(b["coin"]).upper()] = {"free": float(b.get("free", 0) or 0), "locked": float(b.get("locked", 0) or 0)}
        except Exception:
            continue
    return out


def pionex_symbol_rules(symbol):
    default = {"min_amount": 10.0, "min_qty": 0.0, "base_scale": 4, "ok": False}
    j = safe_get_json(f"{PIONEX_BASE}/api/v1/common/symbols", params={"symbol": symbol})
    try:
        s = j["data"]["symbols"][0]
        scale = s.get("baseScale")
        if scale is None:
            scale = s.get("basePrecision", 4)
        return {
            "min_amount": float(s.get("minAmount") or 10.0),
            "min_qty": float(s.get("minQuantity") or s.get("minTradeSize") or 0.0),
            "base_scale": int(scale),
            "ok": True,
        }
    except Exception:
        return default


def fetch_pionex_prices():
    j = safe_get_json(f"{PIONEX_BASE}/api/v1/market/tickers")
    out = {}
    try:
        for t in j["data"]["tickers"]:
            p = float(t.get("close") or 0)
            if p > 0:
                out[str(t["symbol"])] = p
    except Exception:
        pass
    return out


def pionex_place_market_order(symbol, side, size=None, amount=None):
    body = {"symbol": symbol, "side": side.upper(), "type": "MARKET",
            "clientOrderId": f"fs{int(time.time())}{random.randint(100, 999)}"}
    if side.upper() == "BUY":
        body["amount"] = f"{amount:.2f}"
    else:
        body["size"] = str(size)
    r = pionex_request("POST", "/api/v1/trade/order", body=body)
    if r["ok"]:
        oid = str((r["data"] or {}).get("orderId", "OK"))
        return {"ok": True, "order_id": oid, "msg": f"Ordine Pionex ID {oid}"}
    return {"ok": False, "order_id": "", "msg": f"Rifiuto/errore Pionex: {r['msg']}"}


def live_buy(coin_cfg, usdt, price):
    """Acquisto con riconciliazione dei saldi: entry/quantita' REALI, non stimate."""
    sym, base = coin_cfg["pionex"], coin_cfg["code"]
    if not pionex_live_enabled():
        amount = floor_to_scale(usdt / price * (1 - FEE_PCT_PER_SIDE / 100.0), max(coin_cfg["prec"], 2))
        return {"ok": True, "mode": "SIMULATION", "entry_price": price, "amount_coin": amount,
                "invested_usdt": usdt, "order_id": "SIM", "msg": "Simulazione (nessun ordine reale)"}

    rules = pionex_symbol_rules(sym)
    bal0 = pionex_get_balances()
    if bal0 is None:
        return {"ok": False, "mode": "LIVE_ERROR", "msg": "Impossibile leggere i saldi: acquisto annullato"}
    usdt_free = bal0.get("USDT", {}).get("free", 0.0)
    if usdt_free < usdt:
        usdt = floor_to_scale(usdt_free * 0.995, 2)
    if usdt < max(rules["min_amount"], MIN_ORDER_USDT):
        return {"ok": False, "mode": "LIVE_ERROR", "msg": f"USDT liberi insufficienti ({usdt_free:.2f}) / minimo ordine"}

    res = pionex_place_market_order(sym, "BUY", amount=usdt)
    base0 = bal0.get(base, {}).get("free", 0.0)

    base_delta, spent, bal1 = 0.0, 0.0, None
    for _ in range(2):  # riconcilia anche se la risposta e' ambigua (timeout di rete)
        time.sleep(2.0)
        bal1 = pionex_get_balances()
        if bal1 is not None:
            base_delta = bal1.get(base, {}).get("free", 0.0) - base0
            spent = bal0.get("USDT", {}).get("free", 0.0) - bal1.get("USDT", {}).get("free", 0.0)
            if base_delta * price >= 0.5 * usdt:
                break

    filled = base_delta * price >= 0.5 * usdt
    if not filled and res["ok"] and bal1 is None:  # ordine accettato ma saldi illeggibili: stima prudente
        base_delta = usdt / price * (1 - FEE_PCT_PER_SIDE / 100.0)
        spent, filled = usdt, True
    if not filled:
        return {"ok": False, "mode": "LIVE_ERROR", "msg": res["msg"] if not res["ok"] else "Ordine non risulta eseguito"}

    if spent <= 0:
        spent = usdt
    gross = base_delta / (1 - FEE_PCT_PER_SIDE / 100.0)
    entry = spent / gross if gross > 0 else price
    amount = floor_to_scale(base_delta, rules["base_scale"])
    return {"ok": True, "mode": "LIVE_PIONEX", "entry_price": entry, "amount_coin": amount,
            "invested_usdt": spent, "order_id": res["order_id"] or "RECONCILED",
            "msg": f"{res['msg']} | fill reale {amount} {base} per {spent:.2f} USDT"}


def live_sell(pos, coin_cfg, price):
    """Vendita con quantita' limitata al saldo reale. Se fallisce NON si chiude la posizione nello stato."""
    base, sym = coin_cfg["code"], coin_cfg["pionex"]
    invested = pos["invested_usdt"]
    if pos.get("mode") != "LIVE_PIONEX" or not pionex_live_enabled():
        proceeds = pos["amount_coin"] * price * (1 - FEE_PCT_PER_SIDE / 100.0)
        return {"ok": True, "exit_price": price, "pnl_usdt": proceeds - invested, "msg": "Chiusura simulata"}

    rules = pionex_symbol_rules(sym)
    bal0 = pionex_get_balances()
    if bal0 is None:
        return {"ok": False, "msg": "Impossibile leggere i saldi: vendita non inviata (riprovo al prossimo giro)"}
    free = bal0.get(base, {}).get("free", 0.0)
    size = floor_to_scale(min(pos["amount_coin"], free), rules["base_scale"])
    if size <= 0 or size * price < rules["min_amount"] * 0.98:
        return {"ok": False, "msg": f"Quantita' vendibile {size} {base} sotto il minimo: chiudi a mano su Pionex"}

    res = pionex_place_market_order(sym, "SELL", size=fmt_qty(size, rules["base_scale"]))
    usdt0 = bal0.get("USDT", {}).get("free", 0.0)
    sold, proceeds = False, 0.0
    for _ in range(2):
        time.sleep(2.0)
        bal1 = pionex_get_balances()
        if bal1 is not None:
            proceeds = bal1.get("USDT", {}).get("free", 0.0) - usdt0
            if bal1.get(base, {}).get("free", 0.0) <= free - 0.5 * size:
                sold = True
                break
    if not sold and res["ok"] and bal1 is None:
        sold, proceeds = True, size * price * (1 - FEE_PCT_PER_SIDE / 100.0)
    if not sold:
        return {"ok": False, "msg": res["msg"] if not res["ok"] else "Vendita non risulta eseguita"}
    exit_price = proceeds / (size * (1 - FEE_PCT_PER_SIDE / 100.0)) if size > 0 and proceeds > 0 else price
    return {"ok": True, "exit_price": exit_price, "pnl_usdt": proceeds - invested, "msg": res["msg"]}


# ==============================================================================
# 4. ACQUISIZIONE DATI
# ==============================================================================
def fetch_eur_usd_rate():
    data = safe_get_json("https://api.kraken.com/0/public/Ticker", params={"pair": "EURUSD"})
    try:
        if data and not data.get("error"):
            res = data["result"]
            return float(res[list(res.keys())[0]]["c"][0])
    except Exception:
        pass
    return 1.08


def fetch_kraken_ohlc(pair="SOLUSD", interval=60):
    data = safe_get_json("https://api.kraken.com/0/public/OHLC", params={"pair": pair, "interval": interval})
    if not data or data.get("error"):
        return []
    candles_raw = None
    for k, v in data.get("result", {}).items():
        if k != "last" and isinstance(v, list):
            candles_raw = v
            break
    if not candles_raw:
        return []
    candles = []
    for row in candles_raw:
        try:
            candles.append({"time": int(row[0]), "open": float(row[1]), "high": float(row[2]),
                            "low": float(row[3]), "close": float(row[4]), "vwap": float(row[5]),
                            "volume": float(row[6]), "count": int(row[7])})
        except Exception:
            continue
    return candles


def drop_unfinished(candles, interval_sec):
    """Kraken restituisce l'ultima candela ancora in formazione: va scartata (volume/RSI falsati)."""
    if candles and candles[-1]["time"] + interval_sec > time.time():
        return candles[:-1]
    return candles


def build_4h_from_1h(candles_1h):
    if not candles_1h:
        return []
    buckets = {}
    for c in candles_1h:
        b_time = (c["time"] // 14400) * 14400
        b = buckets.get(b_time)
        if b is None:
            buckets[b_time] = {"time": b_time, "open": c["open"], "high": c["high"], "low": c["low"],
                               "close": c["close"], "vwap": c["vwap"], "volume": c["volume"], "count": c["count"]}
        else:
            b["high"] = max(b["high"], c["high"])
            b["low"] = min(b["low"], c["low"])
            b["close"] = c["close"]
            b["volume"] += c["volume"]
            b["count"] += c["count"]
    out = [buckets[k] for k in sorted(buckets)]
    if out and out[-1]["time"] + 3 * 3600 > candles_1h[-1]["time"]:
        out.pop()  # ultimo bucket 4h incompleto
    return out


def fetch_kraken_orderbook(pair="SOLUSD", count=50):
    data = safe_get_json("https://api.kraken.com/0/public/Depth", params={"pair": pair, "count": count})
    default = {"imbalance": 0.0, "spread_bps": 5.0, "bid_ask_ratio": 1.0}
    if not data or data.get("error"):
        return default
    try:
        res = data.get("result", {})
        key = list(res.keys())[0]
        bids, asks = res[key].get("bids", []), res[key].get("asks", [])
        if not bids or not asks:
            return default
        best_bid, best_ask = float(bids[0][0]), float(asks[0][0])
        mid = (best_bid + best_ask) / 2.0
        spread_bps = ((best_ask - best_bid) / mid) * 10000.0 if mid > 0 else 5.0
        bid_w = 0.0
        for b in bids:
            p, v = float(b[0]), float(b[1])
            bid_w += p * v * math.exp(-max(0.0001, (mid - p) / mid) * 100.0)
        ask_w = 0.0
        for a in asks:
            p, v = float(a[0]), float(a[1])
            ask_w += p * v * math.exp(-max(0.0001, (p - mid) / mid) * 100.0)
        total = bid_w + ask_w
        return {"imbalance": (bid_w - ask_w) / total if total > 0 else 0.0, "spread_bps": spread_bps,
                "bid_ask_ratio": bid_w / ask_w if ask_w > 0 else 1.0}
    except Exception:
        return default


def fetch_all_kraken_futures():
    data = safe_get_json("https://futures.kraken.com/derivatives/api/v3/tickers")
    futures_map = {}
    if not data or "tickers" not in data:
        return futures_map
    try:
        for t in data["tickers"]:
            sym = str(t.get("symbol", "")).upper()
            fr_raw = float(t.get("fundingRate", 0.0) or 0.0)
            oi = float(t.get("openInterest", 0.0) or 0.0)
            mark = float(t.get("markPrice", 0.0) or 0.0)
            fr_8h_pct = fr_raw * 8.0 * 100.0 if (abs(fr_raw) < 0.0005 and mark > 0) else fr_raw * 100.0
            futures_map[sym] = {"funding_8h_pct": clamp(fr_8h_pct, -0.5, 0.5), "open_interest": oi, "available": True}
    except Exception:
        pass
    return futures_map


def fetch_fear_and_greed():
    data = safe_get_json("https://api.alternative.me/fng/?limit=1")
    try:
        if data and data.get("data"):
            item = data["data"][0]
            return int(item.get("value", 50)), item.get("value_classification", "Neutral")
    except Exception:
        pass
    return 50, "Neutral"


def fetch_macro_calendar():
    now_utc = datetime.now(timezone.utc)
    events = []
    data = safe_get_json("https://nfs.faireconomy.media/ff_calendar_thisweek.json")
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
        try:
            r = SESSION.get("https://nfs.faireconomy.media/ff_calendar_thisweek.xml", timeout=HTTP_TIMEOUT)
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
# 5. INDICATORI TECNICI (invariati)
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
        g, l = max(0.0, d), max(0.0, -d)
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
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
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
    upper, lower = mid + mult * std, mid - mult * std
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
        up, dn = h - ph, pl - l
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
        pdi, mdi = 100.0 * (p / a), 100.0 * (m / a)
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
        basic_upper, basic_lower = hl2 + multiplier * atr, hl2 - multiplier * atr
        prev_close = candles[i - 1]["close"]
        if i == 1:
            final_upper, final_lower = basic_upper, basic_lower
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
    return direction, (final_lower if direction == 1 else final_upper)


def calc_hurst_exponent(closes, max_lag=20):
    if len(closes) < 60:
        return 0.50
    log_p = [math.log(max(p, 1e-8)) for p in closes[-120:]]
    x_vals, y_vals = [], []
    for lag in range(2, min(max_lag, len(log_p) // 3)):
        diffs = [log_p[i] - log_p[i - lag] for i in range(lag, len(log_p))]
        std = statistics.pstdev(diffs)
        if std > 0:
            x_vals.append(math.log(lag))
            y_vals.append(math.log(std))
    if len(x_vals) < 3:
        return 0.50
    mx, my = sum(x_vals) / len(x_vals), sum(y_vals) / len(y_vals)
    num = sum((x - mx) * (y - my) for x, y in zip(x_vals, y_vals))
    den = sum((x - mx) ** 2 for x in x_vals)
    return clamp(num / den if den > 0 else 0.50, 0.20, 0.85)


def calc_linreg_zscore(closes, window=40):
    if len(closes) < window:
        return 0.0, 0.0
    y = closes[-window:]
    x = list(range(window))
    mx, my = (window - 1) / 2.0, sum(y) / window
    num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    den = sum((xi - mx) ** 2 for xi in x)
    slope = num / den if den > 0 else 0.0
    intercept = my - slope * mx
    residuals = [yi - (slope * xi + intercept) for xi, yi in zip(x, y)]
    std_res = statistics.pstdev(residuals)
    z_score = residuals[-1] / std_res if std_res > 0 else 0.0
    return z_score, ((slope / my) * 100.0 if my > 0 else 0.0)


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
        idx = int(clamp(int((typical - low_min) / step), 0, bins - 1))
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
    mean_v, std_v = sum(vols) / len(vols), statistics.pstdev(vols)
    return (candles[-1]["volume"] - mean_v) / std_v if std_v > 0 else 0.0


# ==============================================================================
# 6. ML (MLP con holdout temporale + Lorentzian k-NN + Monte Carlo)
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
        mv, sv = sum(v_win) / len(v_win), statistics.pstdev(v_win)
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
        dist = sum(math.log(1.0 + abs(a - b)) for a, b in zip(curr_x, features[i]))
        future_ret = closes[i + horizon] - closes[i]
        threshold = 0.15 * atrs[i]
        label = 1.0 if future_ret > threshold else (0.0 if future_ret < -threshold else 0.5)
        candidates.append((dist, label))
    candidates.sort(key=lambda x: x[0])
    nearest = candidates[:k]
    if not nearest:
        return 0.50
    ws = wt = 0.0
    for dist, label in nearest:
        w = 1.0 / (dist + 0.05)
        ws += w * label
        wt += w
    return clamp(ws / wt if wt > 0 else 0.50, 0.05, 0.95)


def online_mlp_neural_net(candles_1h, features, horizon=4, epochs=35, lr=0.045):
    """Ritorna (prob, holdout_acc). La previsione e' ridotta verso 0.5 se il modello non batte
    il baseline (classe maggioritaria) sul 25% piu' recente dei dati: niente 'AI' non validata."""
    n = len(candles_1h)
    if n < 120:
        return 0.50, 0.50
    closes = [c["close"] for c in candles_1h]
    dataset = []
    for i in range(max(35, n - 320), n - horizon - 1):
        dataset.append((features[i], 1.0 if closes[i + horizon] > closes[i] else 0.0))
    if len(dataset) < 60:
        return 0.50, 0.50

    split = int(len(dataset) * 0.75)
    train, hold = dataset[:split], dataset[split:]
    rng = random.Random(42)
    in_dim, hid_dim = 6, 8
    W1 = [[rng.uniform(-0.35, 0.35) for _ in range(in_dim)] for _ in range(hid_dim)]
    b1 = [0.0] * hid_dim
    W2 = [rng.uniform(-0.35, 0.35) for _ in range(hid_dim)]
    b2 = 0.0

    def forward(x):
        h = [math.tanh(clamp(sum(W1[j][i] * x[i] for i in range(in_dim)) + b1[j], -10.0, 10.0)) for j in range(hid_dim)]
        return sigmoid(sum(W2[j] * h[j] for j in range(hid_dim)) + b2), h

    for ep in range(epochs):
        step_lr = lr * (0.97 ** ep)
        for x, y in train:
            y_hat, h_act = forward(x)
            dz2 = y_hat - y
            for j in range(hid_dim):
                grad_w2 = dz2 * h_act[j] + 0.001 * W2[j]
                dh = dz2 * W2[j] * (1.0 - h_act[j] ** 2)
                W2[j] -= step_lr * grad_w2
                b1[j] -= step_lr * dh
                for i in range(in_dim):
                    W1[j][i] -= step_lr * (dh * x[i] + 0.001 * W1[j][i])
            b2 -= step_lr * dz2

    hits = sum(1 for x, y in hold if (forward(x)[0] > 0.5) == (y == 1.0))
    acc = hits / len(hold)
    up_rate = sum(y for _, y in hold) / len(hold)
    edge = acc - max(up_rate, 1.0 - up_rate)
    shrink = clamp(edge / 0.06, 0.0, 1.0)
    raw = forward(features[-1])[0]
    return clamp(0.5 + (raw - 0.5) * shrink, 0.05, 0.95), acc


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
            p *= math.exp(rng.choice(rets))
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
# 7. SKILL OPERATIVE
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
    top = candidates[:20]
    wins, total_r = 0, 0.0
    for _, idx in top:
        entry = closes[idx]
        risk = max(atrs[idx] * 1.15, entry * 0.01)
        tp, sl = entry + 1.45 * risk, entry - 1.0 * risk
        outcome_r = None
        for h in range(1, horizon_bars + 1):
            if lows[idx + h] <= sl:
                outcome_r = -1.0
                break
            if highs[idx + h] >= tp:
                outcome_r = 1.45
                break
        if outcome_r is None:
            outcome_r = clamp((closes[idx + horizon_bars] - entry) / risk, -1.0, 1.45)
        if outcome_r > 0:
            wins += 1
        total_r += outcome_r
    n = len(top)
    return {"win_rate": (wins / n) * 100.0 if n else 50.0, "expectancy_r": total_r / n if n else 0.0, "matches": n}


def skill_chart_scanner_entry_timer(candles_15m, vol_z_1h, ob_imbalance):
    if len(candles_15m) < 25:
        return {"triggered": False, "triggers": 0, "vwap_15m": 0.0, "summary": "Dati 15m insufficienti"}
    closes_15m = [c["close"] for c in candles_15m]
    c15, o15, p15 = closes_15m[-1], candles_15m[-1]["open"], closes_15m[-2]
    ema20_15m = calc_ema(closes_15m, 20)
    rsi_s = calc_rsi_series(closes_15m, 14)
    rsi_15m = rsi_s[-1]
    rsi_15m_prev = rsi_s[-2] if len(rsi_s) >= 2 else rsi_15m
    vwap_15m = calc_rolling_vwap(candles_15m, 96)

    triggers, notes = 0, []
    vwap_diff_pct = (c15 - vwap_15m) / vwap_15m if vwap_15m > 0 else 0.0
    if -0.015 <= vwap_diff_pct <= 0.028 and (c15 >= o15 or c15 >= p15):
        triggers += 1
        notes.append("VWAP Active")
    if c15 >= ema20_15m * 0.997 and rsi_15m >= rsi_15m_prev - 1.5:
        triggers += 1
        notes.append("EMA20 15m Hold/Bounce")
    if vol_z_1h >= -0.20:
        triggers += 1
        notes.append(f"Vol Active (Z={vol_z_1h:+.2f})")
    if ob_imbalance >= 0.01:
        triggers += 1
        notes.append(f"L2 Bid ({ob_imbalance * 100:+.1f}%)")

    rsi_ok = 28.0 <= rsi_15m <= 72.0
    triggered = (triggers >= 2) and rsi_ok
    if not rsi_ok:
        notes.append(f"RSI 15m estremo ({rsi_15m:.0f})")
    return {"triggered": triggered, "triggers": triggers, "vwap_15m": vwap_15m, "ema20_15m": ema20_15m,
            "rsi_15m": rsi_15m, "rsi_15m_prev": rsi_15m_prev,
            "summary": " + ".join(notes) if notes else "In attesa trigger 15m"}


def skill_budget_and_risk_manager(invest_eur, eur_usd, entry_price, sl_price, tp_price, state, macro_high_soon, code=None):
    total_usdt = invest_eur * eur_usd
    cash_buffer_usdt = total_usdt * (CASH_BUFFER_PCT / 100.0)
    usable_usdt = total_usdt - cash_buffer_usdt

    sl_dist_pct = ((entry_price - sl_price) / entry_price) * 100.0 if entry_price > 0 else 2.0
    tp_dist_pct = ((tp_price - entry_price) / entry_price) * 100.0 if entry_price > 0 else 2.5
    fee_rt = PIONEX_FEE_ROUNDTRIP_PCT
    rr_ratio = (tp_dist_pct - fee_rt) / (sl_dist_pct + fee_rt) if sl_dist_pct > 0 else 0.0  # NETTO commissioni

    # Position sizing a rischio fisso: perdita massima (SL + commissioni) = RISK_PER_TRADE_PCT del capitale
    risk_usdt = total_usdt * (RISK_PER_TRADE_PCT / 100.0)
    position_usdt = risk_usdt / ((sl_dist_pct + fee_rt) / 100.0) if sl_dist_pct > 0 else usable_usdt
    position_usdt = min(position_usdt, usable_usdt)
    if usable_usdt >= MIN_ORDER_USDT:
        position_usdt = max(position_usdt, MIN_ORDER_USDT)
    max_loss_usdt = position_usdt * ((sl_dist_pct + fee_rt) / 100.0)
    expected_gain_usdt = position_usdt * ((tp_dist_pct - fee_rt) / 100.0)

    now = datetime.now(timezone.utc)
    now_ts = int(now.timestamp())
    stats = state["stats"]
    today_str = now.strftime("%Y-%m-%d")
    if stats.get("daily_date") != today_str:
        stats.update({"daily_date": today_str, "daily_pnl_pct": 0.0, "daily_pnl_usdt": 0.0,
                      "daily_trades": 0, "consec_losses": 0})

    reasons = []
    if usable_usdt < MIN_ORDER_USDT:
        reasons.append(f"Capitale utilizzabile {usable_usdt:.2f} USDT < minimo ordine")
    if rr_ratio < MIN_REWARD_RISK_R:
        reasons.append(f"R:R netto basso ({rr_ratio:.2f}x < {MIN_REWARD_RISK_R}x)")
    if stats.get("daily_pnl_pct", 0.0) <= -DAILY_MAX_LOSS_PCT:
        reasons.append(f"Circuit Breaker Giornaliero ({stats['daily_pnl_pct']:+.2f}% del capitale)")
    if stats.get("daily_trades", 0) >= MAX_TRADES_PER_DAY:
        reasons.append(f"Max {MAX_TRADES_PER_DAY} trade giornalieri raggiunti")
    if stats.get("consec_losses", 0) >= MAX_CONSEC_LOSSES:
        reasons.append(f"{MAX_CONSEC_LOSSES} stop consecutivi: stop per oggi")
    if macro_high_soon:
        reasons.append("News Macro USD High Impact imminente (<2h)")
    if not (ENTRY_START_UTC <= now.hour < ENTRY_END_UTC):
        reasons.append(f"Fuori finestra operativa {ENTRY_START_UTC:02d}-{ENTRY_END_UTC:02d} UTC")
    last_close = stats.get("last_close_ts", 0)
    if last_close and now_ts - last_close < MIN_MINUTES_BETWEEN_TRADES * 60:
        reasons.append(f"Cooldown dopo ultima chiusura ({MIN_MINUTES_BETWEEN_TRADES} min)")
    last_loss = stats.get("last_loss_ts", 0)
    if last_loss and now_ts - last_loss < COOLDOWN_AFTER_LOSS_MIN * 60:
        reasons.append(f"Cooldown dopo perdita ({COOLDOWN_AFTER_LOSS_MIN} min)")
    if code:
        cl = stats.get("coin_last_close", {}).get(code, 0)
        if cl and now_ts - cl < SAME_COIN_COOLDOWN_H * 3600:
            reasons.append(f"{code} chiusa da meno di {SAME_COIN_COOLDOWN_H:.0f}h")

    passed = not reasons
    return {"passed": passed, "status_str": "PASS (Rischio OK)" if passed else "BLOCCATO: " + ", ".join(reasons),
            "total_usdt": total_usdt, "position_usdt": position_usdt, "cash_buffer_usdt": cash_buffer_usdt,
            "rr_ratio": rr_ratio, "sl_dist_pct": sl_dist_pct, "tp_dist_pct": tp_dist_pct,
            "max_loss_usdt": max_loss_usdt, "expected_gain_usdt": expected_gain_usdt}


def skill_exit_planner(entry_price, sl_price, tp_price):
    one_r = max(entry_price - sl_price, entry_price * 0.008)
    be_fee_lock = entry_price * (1.0 + (PIONEX_FEE_ROUNDTRIP_PCT + 0.05) / 100.0)
    return {"one_r": one_r, "be_trigger": entry_price + 0.65 * one_r, "be_fee_lock": be_fee_lock,
            "tp1_trigger": entry_price + 1.0 * one_r, "tp1_lock": be_fee_lock + 0.25 * one_r,
            "tp2_trigger": entry_price + 1.45 * one_r, "tp2_lock": entry_price + 0.85 * one_r,
            "tp3_target": tp_price}


def skill_nyse_bell_and_session():
    """Sessioni calcolate con fuso America/New_York (gestisce ora legale/solare: apertura 14:30 UTC d'inverno)."""
    now_utc = datetime.now(timezone.utc)
    ny = now_utc.astimezone(ZoneInfo("America/New_York"))
    if ny.weekday() < 5:
        open_utc = ny.replace(hour=9, minute=30, second=0, microsecond=0).astimezone(timezone.utc)
        close_utc = ny.replace(hour=16, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        if abs((now_utc - open_utc).total_seconds()) <= 1800:
            return "🔔 NYSE OPENING BELL (Alta Volatilita' USA)"
        if open_utc < now_utc <= close_utc:
            return "🇺🇸 Sessione Wall Street Attiva (Alta Liquidita')"
    m = now_utc.hour * 60 + now_utc.minute
    if 7 * 60 <= m < 13 * 60:
        return "🇪🇺 Sessione Europea Attiva"
    if now_utc.weekday() >= 5:
        return "📅 Weekend (liquidita' ridotta)"
    return "🌏 Sessione Asiatica / Notturna"


# ==============================================================================
# 8. VALUTAZIONE SINGOLA COIN
# ==============================================================================
def evaluate_coin(coin_cfg, futures_map, fng_val, macro_events):
    code, pair = coin_cfg["code"], coin_cfg["kraken"]

    candles_1h = drop_unfinished(fetch_kraken_ohlc(pair, interval=60), 3600)
    candles_15m = drop_unfinished(fetch_kraken_ohlc(pair, interval=15), 900)
    candles_4h = build_4h_from_1h(candles_1h)
    ob = fetch_kraken_orderbook(pair, count=50)

    if len(candles_1h) < 60 or len(candles_4h) < 30 or len(candles_15m) < 30:
        return None

    fut = futures_map.get(coin_cfg["futures"], {"funding_8h_pct": 0.005, "open_interest": 0.0, "available": False})
    closes_4h = [c["close"] for c in candles_4h]
    closes_1h = [c["close"] for c in candles_1h]
    closes_15m = [c["close"] for c in candles_15m]

    # Prezzo: ticker live Pionex (venue di esecuzione) con controllo di coerenza vs Kraken
    ref_price = closes_15m[-1]
    px = LIVE_PX.get(coin_cfg["pionex"])
    price = px if (px and abs(px / ref_price - 1.0) < 0.03) else ref_price

    ema20_4h, ema50_4h = calc_ema(closes_4h, 20), calc_ema(closes_4h, 50)
    ema20_1h, ema50_1h = calc_ema(closes_1h, 20), calc_ema(closes_1h, 50)
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
    adx_1h, _, _ = calc_adx(candles_1h, 14)
    st_dir_1h, _ = calc_supertrend(candles_1h, 10, 3.0)
    hurst = calc_hurst_exponent(closes_1h, 20)
    z_linreg, _ = calc_linreg_zscore(closes_1h, 40)
    poc_price = calc_volume_profile_poc(candles_1h, 24, 120)
    vol_z = calc_volume_zscore(candles_1h, 30)

    recent_lows = [c["low"] for c in candles_15m[-16:]]
    swing_low = min(recent_lows) if recent_lows else price * 0.985
    recent_highs = [c["high"] for c in candles_1h[-24:]]
    swing_high = max(recent_highs) if recent_highs else price * 1.025

    sl_dist = max(price * 0.009, min(price * 0.018, 1.15 * atr_1h))
    sl_price = max(price - sl_dist, swing_low - 0.15 * atr_1h)
    sl_price = clamp(sl_price, price * 0.980, price * 0.991)
    actual_risk = price - sl_price
    tp_candidate = max(price + actual_risk * 1.35, min(swing_high * 0.998, price + 2.2 * atr_1h))
    tp_price = clamp(tp_candidate, price * 1.0135, price * 1.025)

    features = build_feature_matrix(candles_1h)
    prob_knn = lorentzian_knn_predict(candles_1h, features, horizon=4, k=18)
    prob_mlp, mlp_acc = online_mlp_neural_net(candles_1h, features, horizon=4, epochs=35, lr=0.045)
    prob_mc = monte_carlo_Barrier_sim(candles_1h, price, tp_price, sl_price, paths=300, horizon_bars=10)
    ai_ensemble_prob = 0.40 * prob_mlp + 0.35 * prob_knn + 0.25 * prob_mc

    tm_stats = skill_time_machine_backtest(candles_1h, features, horizon_bars=6)
    scanner_timer = skill_chart_scanner_entry_timer(candles_15m, vol_z, ob["imbalance"])

    # A. Trend (max 25)
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

    # B. AI + Time Machine (max 25)
    ai_pts = round(clamp((ai_ensemble_prob - 0.35) / 0.35, 0.0, 1.0) * 18.0)
    tm_pts = 7 if (tm_stats["win_rate"] >= 52.0 and tm_stats["expectancy_r"] > 0.05) else (4 if tm_stats["win_rate"] >= 46.0 else 2)
    score_ai = clamp(ai_pts + tm_pts, 0, 25)

    # C. Momentum (max 20)
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

    # D. Microstruttura (max 15)
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

    # E. Derivati / sentiment / macro (max 15)
    score_macro = 0
    fr = fut["funding_8h_pct"]
    if -0.02 <= fr <= 0.025:
        score_macro += 5
    elif fr < -0.02:
        score_macro += 6
    else:
        score_macro += 2
    score_macro += 5 if 20 <= fng_val <= 78 else 3
    high_soon = [e for e in macro_events if e["impact"] == "High" and -0.5 <= e["diff_hours"] <= 2.0]
    if not high_soon:
        score_macro += 5
    score_macro = clamp(score_macro, 0, 15)

    raw_100 = score_trend + score_ai + score_mom + score_micro + score_macro
    final_score = clamp(int(round(raw_100 / 10.0)), 1, 10)

    return {
        "coin": coin_cfg, "code": code, "price": price, "sl_price": sl_price, "tp_price": tp_price,
        "atr_1h": atr_1h, "atr_pct": atr_pct, "raw_100": raw_100, "final_score": final_score,
        "score_trend": score_trend, "score_ai": score_ai, "score_mom": score_mom,
        "score_micro": score_micro, "score_macro": score_macro,
        "ai_prob": ai_ensemble_prob, "prob_mlp": prob_mlp, "prob_knn": prob_knn, "prob_mc": prob_mc,
        "mlp_holdout_acc": mlp_acc, "rsi_1h": rsi_1h, "rsi_4h": rsi_4h, "rsi_15m": rsi_15m,
        "rsi_15m_prev": rsi_15m_prev, "adx_1h": adx_1h, "hurst": hurst, "z_linreg": z_linreg,
        "poc_price": poc_price, "vol_z": vol_z, "ob": ob, "fut": fut, "st_dir_1h": st_dir_1h,
        "tm_stats": tm_stats, "scanner_timer": scanner_timer, "high_soon": bool(high_soon),
        "c15_recent": [{"time": c["time"], "high": c["high"], "low": c["low"]} for c in candles_15m[-24:]],
    }


def make_fallback_eval(coin_cfg, pos):
    """Valutazione neutra se l'analisi della coin aperta fallisce: gestisce solo SL/TP sul prezzo Pionex.
    Mai usare il prezzo di un'altra coin."""
    px = LIVE_PX.get(coin_cfg["pionex"])
    price = px or pos.get("entry_price", 0.0)
    return {
        "coin": coin_cfg, "code": coin_cfg["code"], "price": price,
        "sl_price": pos.get("sl_price", 0.0), "tp_price": pos.get("tp_price", 0.0),
        "atr_1h": 0.0, "atr_pct": 0.0, "raw_100": 0, "final_score": 5, "ai_prob": 0.5,
        "prob_mlp": 0.5, "prob_knn": 0.5, "prob_mc": 0.5, "mlp_holdout_acc": 0.5,
        "rsi_1h": 50.0, "rsi_4h": 50.0, "rsi_15m": 50.0, "rsi_15m_prev": 50.0,
        "ob": {"imbalance": 0.0, "spread_bps": 0.0, "bid_ask_ratio": 1.0},
        "tm_stats": {"win_rate": 50.0, "expectancy_r": 0.0, "matches": 0},
        "scanner_timer": {"triggered": False, "triggers": 0, "vwap_15m": 0.0, "summary": "Dati non disponibili"},
        "high_soon": False, "c15_recent": [], "stale": not bool(px), "fallback": True,
    }


def scan_all_coins(coins, futures_map, fng_val, macro_events):
    results = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_to_code = {executor.submit(evaluate_coin, cfg, futures_map, fng_val, macro_events): cfg["code"] for cfg in coins}
        for future in as_completed(future_to_code):
            code = future_to_code[future]
            try:
                res = future.result()
                if res:
                    results[code] = res
            except Exception as e:
                log("WARN", f"Errore analisi {code}: {e}")
    return results


# ==============================================================================
# 9. MOTORE AUTONOMO
# ==============================================================================
def entry_conditions(ev):
    rm = ev.get("risk_manager")
    return bool(
        not ev.get("fallback") and rm and rm["passed"]
        and ev["final_score"] >= Entry_Score_Min and ev["raw_100"] >= Entry_Raw_100_Min
        and ev["ai_prob"] >= 0.50 and ev["scanner_timer"]["triggered"]
    )


def _finalize_close(state, open_code, coin_cfg, curr_price, close_reason, close, alerts_fired, now_str, now_ts, capital_usdt):
    pos, stats = state["position"], state["stats"]
    entry = pos["entry_price"]
    invested = pos["invested_usdt"]
    pnl_usdt = close["pnl_usdt"]
    net_pnl_pct = (pnl_usdt / invested) * 100.0 if invested > 0 else 0.0
    exit_price = close.get("exit_price", curr_price)

    stats["total_trades"] += 1
    if pnl_usdt >= 0:
        stats["wins"] += 1
        stats["consec_losses"] = 0
    else:
        stats["losses"] += 1
        stats["consec_losses"] = stats.get("consec_losses", 0) + 1
        stats["last_loss_ts"] = now_ts
    stats["total_pnl_pct"] = round(stats["total_pnl_pct"] + net_pnl_pct, 2)
    stats["total_pnl_usdt"] = round(stats["total_pnl_usdt"] + pnl_usdt, 2)
    stats["daily_pnl_usdt"] = round(stats.get("daily_pnl_usdt", 0.0) + pnl_usdt, 2)
    stats["daily_pnl_pct"] = round(stats.get("daily_pnl_pct", 0.0) + (pnl_usdt / capital_usdt) * 100.0, 2)  # % del CAPITALE
    stats["last_close_ts"] = now_ts
    stats.setdefault("coin_last_close", {})[open_code] = now_ts
    stats["last_action"] = f"CHIUSO {open_code} ({net_pnl_pct:+.2f}%) il {now_str}"

    state.setdefault("history", []).insert(0, {
        "coin": open_code, "entry": entry, "exit": exit_price, "pnl_pct": round(net_pnl_pct, 2),
        "pnl_usdt": round(pnl_usdt, 2), "reason": close_reason, "closed_at": now_str, "mode": pos.get("mode", "SIMULATION")})
    state["history"] = state["history"][:25]
    pos["is_open"] = False
    pos["close_fail_count"] = 0
    save_state(state)
    return {"action_state": "CLOSED_NOW", "headline": f"🔔 POSIZIONE {open_code} CHIUSA AUTOMATICAMENTE",
            "detail": f"{close_reason} | PnL Netto: {net_pnl_pct:+.2f}% ({pnl_usdt:+.2f} USDT) | {close['msg']}",
            "pnl_pct": net_pnl_pct, "pnl_usdt": pnl_usdt, "current_r": 0.0, "alerts_fired": alerts_fired}


def _manage_open_position(state, all_evals, capital_usdt, now_str, now_ts):
    pos = state["position"]
    open_code = pos.get("coin_code", "SOL")
    coin_cfg = COIN_MAP.get(open_code, COINS[0])
    open_eval = all_evals[open_code]
    curr_price = open_eval["price"]
    entry, sl, tp = pos["entry_price"], pos["sl_price"], pos["tp_price"]
    one_r = pos.get("one_r") or max(entry - pos.get("initial_sl", sl), entry * 0.009)
    opened_ts = pos.get("opened_ts") or now_ts
    hours_open = max(0.0, (now_ts - opened_ts) / 3600.0)
    alerts_fired, trail_msg = [], ""

    # Riconciliazione: se in LIVE la coin non e' piu' sull'account (chiusa a mano), allinea lo stato
    if pos.get("mode") == "LIVE_PIONEX" and pionex_live_enabled():
        bal = pionex_get_balances()
        if bal is not None:
            held = bal.get(open_code, {"free": 0.0, "locked": 0.0})
            if held["free"] + held["locked"] < pos["amount_coin"] * 0.90:
                est = pos["amount_coin"] * curr_price * (1 - FEE_PCT_PER_SIDE / 100.0) - pos["invested_usdt"]
                close = {"ok": True, "exit_price": curr_price, "pnl_usdt": est, "msg": "Rilevata chiusura esterna (PnL stimato)"}
                return _finalize_close(state, open_code, coin_cfg, curr_price, "🧾 CHIUSA ESTERNAMENTE (saldo coin assente)",
                                       close, alerts_fired, now_str, now_ts, capital_usdt)

    if open_eval.get("stale"):
        save_state(state)
        return {"action_state": "HOLDING", "headline": f"⚠️ {open_code}: PREZZO NON DISPONIBILE",
                "detail": "Analisi e ticker non raggiungibili: gestione SL/TP sospesa fino al prossimo giro.",
                "pnl_pct": 0.0, "pnl_usdt": 0.0, "current_r": 0.0, "alerts_fired": []}

    # Candele 15m chiuse dall'ultimo controllo: intercettano wick tra due esecuzioni (cron ogni 30 min)
    last_chk = pos.get("last_check_ts") or opened_ts
    c15 = open_eval.get("c15_recent", [])
    fresh = [c for c in c15 if c["time"] + 900 > last_chk]
    since_open = [c for c in c15 if c["time"] + 900 > opened_ts]
    wick_low = min((c["low"] for c in fresh), default=curr_price)
    wick_high = max((c["high"] for c in fresh), default=curr_price)
    highest = max(pos.get("highest_price", entry), curr_price, max((c["high"] for c in since_open), default=0.0))
    pos["highest_price"] = highest
    sl_at_start = sl

    est_proceeds = pos["amount_coin"] * curr_price * (1 - FEE_PCT_PER_SIDE / 100.0)
    pnl_usdt_live = est_proceeds - pos["invested_usdt"]
    net_pnl_pct = (pnl_usdt_live / pos["invested_usdt"]) * 100.0 if pos["invested_usdt"] > 0 else 0.0
    current_r = (curr_price - entry) / one_r if one_r > 0 else 0.0

    be_fee_price = round(entry * (1.0 + (PIONEX_FEE_ROUNDTRIP_PCT + 0.05) / 100.0), 6)
    if not pos.get("breakeven_locked", False) and (current_r >= 0.65 or (curr_price >= entry * 1.0055 and open_eval["rsi_15m"] >= 66.0)):
        if sl < be_fee_price:
            pos["sl_price"] = sl = be_fee_price
        pos["breakeven_locked"] = True
        alerts_fired.append(f"🔔 Fast-Scalp Lock: Stop-Loss portato a Break-Even Netto ({fmt_price(sl)})")
        trail_msg = "🛡️ Break-Even Netto Attivato"
    if not pos.get("tp1_locked", False) and current_r >= 1.0:
        new_sl = round(be_fee_price + 0.30 * one_r, 6)
        if new_sl > sl:
            pos["sl_price"] = sl = new_sl
        pos["tp1_locked"] = True
        alerts_fired.append(f"🔔 Alert +1.0R: Profitto blindato a {fmt_price(sl)}")
        trail_msg = f"🔒 Profitto +1R Blindato ({fmt_price(sl)})"
    if not pos.get("tp2_locked", False) and current_r >= 1.45:
        new_sl = round(entry + 0.85 * one_r, 6)
        if new_sl > sl:
            pos["sl_price"] = sl = new_sl
        pos["tp2_locked"] = True
        alerts_fired.append(f"🔔 Alert +1.45R: Trailing Stop alzato a {fmt_price(sl)}")
        trail_msg = f"🚀 Trailing +1.45R ({fmt_price(sl)})"
    if current_r >= 1.90 and "R190" not in pos.get("alerts_triggered", []):
        new_sl = round(entry + 1.35 * one_r, 6)
        if new_sl > sl:
            pos["sl_price"] = sl = new_sl
        pos.setdefault("alerts_triggered", []).append("R190")
        alerts_fired.append(f"🔔 Alert +1.9R: Super-Trailing a {fmt_price(sl)}")

    rsi_15m = open_eval.get("rsi_15m", 50.0)
    rsi_15m_prev = open_eval.get("rsi_15m_prev", rsi_15m)
    ob_imb = open_eval["ob"]["imbalance"]
    momentum_fading_15m = (
        (rsi_15m_prev >= 61.0 and rsi_15m < rsi_15m_prev - 4.0)
        or (ob_imb <= -0.10 and open_eval["ai_prob"] < 0.50)
        or (highest >= entry + 1.0 * one_r and curr_price <= highest - 0.40 * one_r)
    )

    close_reason = None
    if wick_low <= sl_at_start or curr_price <= sl:
        kind = "PROFIT LOCK / TRAILING STOP" if sl_at_start > entry else "STOP LOSS"
        tag = " (toccato tra due controlli)" if curr_price > sl and wick_low <= sl_at_start else ""
        close_reason = f"{'🔒' if sl_at_start > entry else '🛑'} {kind} ({fmt_price(curr_price)}){tag}"
    elif curr_price >= tp or wick_high >= tp:
        close_reason = f"🎯 TAKE PROFIT RAGGIUNTO ({fmt_price(curr_price)})"
    elif net_pnl_pct >= SCALP_MIN_NET_PROFIT_PCT and momentum_fading_15m:
        close_reason = f"💰 FAST-SCALP PROFIT TAKE (+{net_pnl_pct:.2f}% netto su rallentamento 15m)"
    elif hours_open >= TIME_STOP_PROFIT_HOURS and net_pnl_pct >= 0.35:
        close_reason = f"⏱️ TIME-STOP SCALP IN PROFITTO (+{net_pnl_pct:.2f}% dopo {hours_open:.1f}h)"
    elif hours_open >= TIME_STOP_STALE_HOURS and open_eval["ai_prob"] < 0.46 and net_pnl_pct > -0.60:
        close_reason = f"⏱️ ROTAZIONE CAPITALE ({hours_open:.1f}h in stallo, AI={open_eval['ai_prob'] * 100:.0f}%)"
    elif open_eval["final_score"] <= 3 and open_eval["ai_prob"] < 0.40:
        close_reason = f"⚠️ USCITA DIFENSIVA AI (Score {open_code} sceso a {open_eval['final_score']}/10)"

    if close_reason:
        close = live_sell(pos, coin_cfg, curr_price)
        if not close["ok"]:
            pos["close_fail_count"] = pos.get("close_fail_count", 0) + 1
            pos["last_check_ts"] = now_ts
            state["stats"]["last_action"] = f"CHIUSURA {open_code} FALLITA ({now_str})"
            save_state(state)
            return {"action_state": "CLOSE_FAILED",
                    "headline": f"🚨 CHIUSURA {open_code} FALLITA - POSIZIONE ANCORA APERTA",
                    "detail": f"{close_reason} | {close['msg']} | Tentativo #{pos['close_fail_count']}. Verifica subito su Pionex!",
                    "pnl_pct": net_pnl_pct, "pnl_usdt": pnl_usdt_live, "current_r": current_r, "alerts_fired": alerts_fired}
        return _finalize_close(state, open_code, coin_cfg, curr_price, close_reason, close, alerts_fired, now_str, now_ts, capital_usdt)

    pos["last_check_ts"] = now_ts
    save_state(state)
    extra = f" | {trail_msg}" if trail_msg else ""
    return {"action_state": "HOLDING", "headline": f"⏳ MANTENIMENTO POSIZIONE LONG SU {open_code} ({pos['mode']})",
            "detail": f"Entry {fmt_price(entry)} -> Attuale {fmt_price(curr_price)} ({net_pnl_pct:+.2f}% netto / {current_r:+.2f}R){extra}",
            "pnl_pct": net_pnl_pct, "pnl_usdt": pnl_usdt_live, "current_r": current_r, "alerts_fired": alerts_fired}


def _try_open_position(state, ranked_evals, now_str, now_ts):
    pos, stats = state["position"], state["stats"]
    candidates = [ev for ev in ranked_evals if entry_conditions(ev)]

    if not candidates:
        save_state(state)
        best = ranked_evals[0]
        rm = best["risk_manager"]
        why = []
        if best["raw_100"] < Entry_Raw_100_Min:
            why.append(f"Score {best['code']} {best['raw_100']}/100 < {Entry_Raw_100_Min}/100")
        if best["ai_prob"] < 0.50:
            why.append(f"AI {best['ai_prob'] * 100:.0f}% < 50%")
        if not best["scanner_timer"]["triggered"]:
            why.append(f"Timer 15m ({best['scanner_timer']['triggers']}/4 trigger)")
        if not rm["passed"]:
            why.append(rm["status_str"])
        return {"action_state": "WAITING", "headline": "🛡️ CAPITALE PROTETTO IN USDT (Nessun acquisto ora)",
                "detail": f"Migliore: {best['code']} ({best['final_score']}/10 - {best['raw_100']}/100) -> Non apro perche': {' | '.join(why) or 'In attesa di conferma'}.",
                "pnl_pct": 0.0, "pnl_usdt": 0.0, "current_r": 0.0, "alerts_fired": []}

    best = candidates[0]
    return _execute_open(state, best, round(best["risk_manager"]["position_usdt"], 2), now_str, now_ts)


def _execute_open(state, ev, usdt, now_str, now_ts, label="FAST-SCALP"):
    pos, stats = state["position"], state["stats"]
    coin_cfg, code, price = ev["coin"], ev["code"], ev["price"]
    buy = live_buy(coin_cfg, usdt, price)
    if not buy["ok"]:
        stats["last_action"] = f"ACQUISTO {code} FALLITO ({now_str})"
        save_state(state)
        return {"action_state": "ENTRY_FAILED", "headline": f"🚨 ACQUISTO {code} NON ESEGUITO",
                "detail": f"{buy['msg']} (nessuna posizione registrata)", "pnl_pct": 0.0, "pnl_usdt": 0.0,
                "current_r": 0.0, "alerts_fired": []}

    entry = buy["entry_price"]
    k = entry / price if price > 0 else 1.0       # riallinea SL/TP se il fill differisce dal prezzo di segnale
    sl_price, tp_price = ev["sl_price"] * k, ev["tp_price"] * k
    one_r = max(entry - sl_price, entry * 0.009)
    pos.update({
        "is_open": True, "coin_code": code, "symbol": coin_cfg["pionex"], "entry_price": entry,
        "amount_coin": buy["amount_coin"], "invested_usdt": buy["invested_usdt"], "sl_price": sl_price,
        "tp_price": tp_price, "initial_sl": sl_price, "one_r": one_r, "highest_price": entry,
        "breakeven_locked": False, "tp1_locked": False, "tp2_locked": False, "alerts_triggered": [],
        "opened_at": now_str, "opened_ts": now_ts, "last_check_ts": now_ts, "close_fail_count": 0,
        "mode": buy["mode"], "order_id": buy.get("order_id", "SIM")})
    stats["daily_trades"] = stats.get("daily_trades", 0) + 1
    stats["last_action"] = f"APERTO LONG {code} a {fmt_price(entry)} il {now_str} ({label})"
    save_state(state)  # subito su disco: lo stato non deve MAI andare perso dopo un ordine reale
    return {"action_state": "OPENED_NOW", "headline": f"🚀 NUOVA POSIZIONE {label} APERTA SU {code} ({buy['mode']})",
            "detail": f"Acquistati {buy['amount_coin']} {code} ({buy['invested_usdt']:.2f} USDT) a {fmt_price(entry)} | SL {fmt_price(sl_price)} | TP {fmt_price(tp_price)} | {buy['msg']}",
            "pnl_pct": 0.0, "pnl_usdt": 0.0, "current_r": 0.0,
            "alerts_fired": [f"🔔 Ingresso {label}: posizione aperta su {code} a {fmt_price(entry)}"]}


def run_manual_command(state, all_evals, eur_usd, command, coin, usdt_req):
    """Comandi d'emergenza: ignorano filtri, finestra oraria, cooldown e circuit breaker."""
    pos = state["position"]
    now_dt = datetime.now(timezone.utc)
    now_str, now_ts = now_dt.strftime("%d/%m %H:%M UTC"), int(now_dt.timestamp())
    capital_usdt = INVEST_EUR * eur_usd

    def noop(msg):
        return {"action_state": "MANUAL_NOOP", "headline": "ℹ️ COMANDO MANUALE NON ESEGUITO", "detail": msg,
                "pnl_pct": 0.0, "pnl_usdt": 0.0, "current_r": 0.0, "alerts_fired": []}

    def close_registered():
        code = pos["coin_code"]
        cfg = COIN_MAP.get(code, COINS[0])
        price = all_evals[code]["price"]
        close = live_sell(pos, cfg, price)
        if not close["ok"]:
            pos["close_fail_count"] = pos.get("close_fail_count", 0) + 1
            save_state(state)
            return {"action_state": "CLOSE_FAILED", "headline": f"🚨 CHIUSURA MANUALE {code} FALLITA",
                    "detail": f"{close['msg']} | Chiudi a mano dall'app Pionex!", "pnl_pct": 0.0, "pnl_usdt": 0.0,
                    "current_r": 0.0, "alerts_fired": []}
        return _finalize_close(state, code, cfg, price, "🖐️ CHIUSURA MANUALE D'EMERGENZA", close, [], now_str, now_ts, capital_usdt)

    if command == "close":
        if not pos["is_open"]:
            return noop("Nessuna posizione registrata nel bot. Se hai coin sul conto usa 'sell_all' indicando la coin, oppure chiudi dall'app Pionex.")
        return close_registered()

    if command == "sell_all":
        cfg = COIN_MAP.get(coin)
        if not cfg:
            return noop(f"Coin '{coin}' non gestita dal bot.")
        if pos["is_open"] and pos["coin_code"] == coin:
            return close_registered()
        if not pionex_live_enabled():
            return noop("Vendita totale possibile solo in modalita' LIVE (AUTO_TRADE_ENABLED=true e chiavi presenti).")
        price = LIVE_PX.get(cfg["pionex"]) or (all_evals.get(coin) or {}).get("price", 0.0)
        close = live_sell({"mode": "LIVE_PIONEX", "amount_coin": 1e12, "invested_usdt": 0.0}, cfg, price)
        if not close["ok"]:
            return {"action_state": "CLOSE_FAILED", "headline": f"🚨 VENDITA TOTALE {coin} FALLITA",
                    "detail": f"{close['msg']} | Vendi a mano dall'app Pionex!", "pnl_pct": 0.0, "pnl_usdt": 0.0,
                    "current_r": 0.0, "alerts_fired": []}
        state["stats"]["last_action"] = f"VENDUTO TUTTO {coin} ({now_str}, manuale)"
        save_state(state)
        return {"action_state": "MANUAL_DONE", "headline": f"🖐️ VENDUTO TUTTO IL SALDO {coin}",
                "detail": f"Incassati {close['pnl_usdt']:+.2f} USDT | {close['msg']}",
                "pnl_pct": 0.0, "pnl_usdt": close["pnl_usdt"], "current_r": 0.0, "alerts_fired": []}

    if command == "open":
        if pos["is_open"]:
            return noop(f"C'e' gia' una posizione aperta su {pos['coin_code']}: chiudila prima.")
        ev = all_evals.get(coin)
        if not ev or ev.get("fallback"):
            return noop(f"Analisi di '{coin}' non disponibile: impossibile calcolare SL/TP. Riprova.")
        usdt = usdt_req if usdt_req > 0 else ev["risk_manager"]["position_usdt"]
        usdt = min(usdt, capital_usdt * (1 - CASH_BUFFER_PCT / 100.0))
        if usdt < MIN_ORDER_USDT:
            return noop(f"Importo {usdt:.2f} USDT sotto il minimo ordine ({MIN_ORDER_USDT}).")
        return _execute_open(state, ev, round(usdt, 2), now_str, now_ts, label="MANUALE")

    return noop(f"Comando sconosciuto: '{command}'")


def manage_autonomous_trading(state, ranked_evals, all_evals, eur_usd):
    now_dt = datetime.now(timezone.utc)
    now_str, now_ts = now_dt.strftime("%d/%m %H:%M UTC"), int(now_dt.timestamp())
    capital_usdt = INVEST_EUR * eur_usd
    if state["position"]["is_open"]:
        return _manage_open_position(state, all_evals, capital_usdt, now_str, now_ts)
    return _try_open_position(state, ranked_evals, now_str, now_ts)


# ==============================================================================
# 10. WEB CONSOLE (index.html)
# ==============================================================================
def build_manual_action_advisor(state, best_eval, all_evals, trade_status):
    pos = state["position"]
    if pos["is_open"]:
        code = pos["coin_code"]
        ev = all_evals.get(code, best_eval)
        pnl, curr_r = trade_status["pnl_pct"], trade_status["current_r"]
        if ev["final_score"] <= 4 or ev["ai_prob"] < 0.44:
            return {"badge": "⚠️ CONSIGLIO MANUALE: VALUTA CHIUSURA ANTICIPATA", "color": "#f59e0b", "bg": "rgba(245,158,11,0.14)",
                    "instruction": f"Il punteggio di {code} si sta indebolendo ({ev['final_score']}/10, AI {ev['ai_prob'] * 100:.1f}%). Se operi manualmente su Pionex, valuta di chiudere o stringere lo Stop-Loss."}
        if pnl >= SCALP_MIN_NET_PROFIT_PCT:
            return {"badge": "💰 POSIZIONE IN ZONA SCALP PROFIT", "color": "#10b981", "bg": "rgba(16,185,129,0.14)",
                    "instruction": f"La posizione su {code} e' in profitto netto ({pnl:+.2f}% / {curr_r:+.2f}R). Il bot incassera' automaticamente al primo segnale di inversione 15m o al raggiungimento del TP ({fmt_price(pos['tp_price'])})."}
        return {"badge": "✅ MANTIENI POSIZIONE (HOLD)", "color": "#38bdf8", "bg": "rgba(56,189,248,0.14)",
                "instruction": f"Posizione aperta su {code} (PnL netto {pnl:+.2f}%). Mantieni attiva la protezione con Stop-Loss a {fmt_price(pos['sl_price'])} e Take-Profit a {fmt_price(pos['tp_price'])}."}

    if trade_status.get("action_state") in ("ENTRY_FAILED", "CLOSE_FAILED"):
        return {"badge": "🚨 ERRORE ESECUZIONE ORDINE", "color": "#ef4444", "bg": "rgba(239,68,68,0.14)",
                "instruction": "Controlla subito saldi e ordini su Pionex: " + trade_status["detail"]}

    score, raw_100, code = best_eval["final_score"], best_eval["raw_100"], best_eval["code"]
    rm, tm = best_eval["risk_manager"], best_eval["scanner_timer"]
    if entry_conditions(best_eval):
        return {"badge": f"🚀 SEGNALE ATTIVO: APRI LONG SU {code}", "color": "#10b981", "bg": "rgba(16,185,129,0.15)",
                "instruction": f"Tutti i semafori sono VERDI su {code} ({score}/10 - {raw_100}/100). Se operi manualmente su Pionex: Apri LONG Spot su {best_eval['coin']['pionex']} con {rm['position_usdt']:.2f} USDT, SL {fmt_price(best_eval['sl_price'])} e TP {fmt_price(best_eval['tp_price'])}."}
    if raw_100 >= 54:
        return {"badge": f"👀 QUASI PRONTO SU {code} ({raw_100}/100) - ATTENDI", "color": "#f59e0b", "bg": "rgba(245,158,11,0.14)",
                "instruction": f"{code} e' vicino alla soglia di ingresso ({raw_100}/100), ma manca una conferma ({tm['summary']} | {rm['status_str']}). Attendi il prossimo segnale prima di entrare."}
    return {"badge": "🛡️ RESTA FLAT IN USDT (NON APRIRE ORA)", "color": "#94a3b8", "bg": "rgba(148,163,184,0.12)",
            "instruction": f"Nessuna delle 10 coin offre un vantaggio sufficiente in questo momento (Migliore: {code} con {raw_100}/100). Non aprire posizioni manuali."}


def generate_webapp_html(ranked_evals, state, trade_status, fng_val, fng_class, session_str, eur_usd=1.08):
    focus = next((e for e in ranked_evals if entry_conditions(e)), ranked_evals[0])
    advisor = build_manual_action_advisor(state, focus, {e["code"]: e for e in ranked_evals}, trade_status)
    now_str = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M:%S UTC")

    coins_payload = []
    for e in ranked_evals:
        rm = e.get("risk_manager") or skill_budget_and_risk_manager(INVEST_EUR, eur_usd, e["price"], e["sl_price"], e["tp_price"], state, e["high_soon"], e["code"])
        ep = e.get("exit_plan") or skill_exit_planner(e["price"], e["sl_price"], e["tp_price"])
        coins_payload.append({
            "code": e["code"], "name": e["coin"]["name"], "pionex": e["coin"]["pionex"], "kraken": e["coin"]["kraken"],
            "ws": e["coin"]["ws"], "price": e["price"], "sl_price": e["sl_price"], "tp_price": e["tp_price"],
            "final_score": e["final_score"], "raw_100": e["raw_100"], "ai_prob": round(e["ai_prob"] * 100, 1),
            "prob_mlp": round(e["prob_mlp"] * 100, 1), "prob_knn": round(e["prob_knn"] * 100, 1),
            "prob_mc": round(e["prob_mc"] * 100, 1), "rsi_1h": round(e["rsi_1h"], 1), "rsi_15m": round(e["rsi_15m"], 1),
            "ob_imb": round(e["ob"]["imbalance"] * 100, 1), "tm_wr": round(e["tm_stats"]["win_rate"], 1),
            "tm_exp": round(e["tm_stats"]["expectancy_r"], 2), "timer_ok": e["scanner_timer"]["triggered"],
            "timer_triggers": e["scanner_timer"]["triggers"], "timer_summary": e["scanner_timer"]["summary"],
            "vwap_15m": e["scanner_timer"]["vwap_15m"], "risk_ok": rm["passed"], "risk_status": rm["status_str"],
            "rr_ratio": round(rm["rr_ratio"], 2), "pos_usdt": round(rm["position_usdt"], 2),
            "entry_ok": entry_conditions(e), "be_trigger": ep["be_trigger"], "tp1_trigger": ep["tp1_trigger"],
            "tp2_trigger": ep["tp2_trigger"]})

    payload = {"updated_at": now_str, "session": session_str, "fng_val": fng_val, "fng_class": fng_class,
               "advisor": advisor, "position": state["position"], "stats": state["stats"],
               "history": state.get("history", []), "coins": coins_payload,
               "repo": os.getenv("GITHUB_REPOSITORY", ""), "workflow": "bot.yml", "ref": os.getenv("GITHUB_REF_NAME") or "main",
               "limits": {"max_trades": MAX_TRADES_PER_DAY, "daily_max_loss": DAILY_MAX_LOSS_PCT, "fee_rt": PIONEX_FEE_ROUNDTRIP_PCT}}
    payload_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    html = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pionex AI Live Trading Console v8.0</title>
<style>
  :root { --bg: #070b14; --card: #111827; --card-border: #1f2937; --text: #f8fafc; --muted: #94a3b8;
          --accent: #38bdf8; --green: #10b981; --amber: #f59e0b; --red: #ef4444; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: radial-gradient(circle at top, #131c31 0%, var(--bg) 70%); color: var(--text);
    padding: 14px; max-width: 960px; margin: 0 auto; }
  .topbar { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 10px;
    padding: 12px 16px; background: rgba(17,24,39,0.9); border: 1px solid var(--card-border); border-radius: 14px; margin-bottom: 14px; }
  .brand { font-size: 17px; font-weight: 800; display: flex; align-items: center; gap: 8px; }
  .live-dot { width: 10px; height: 10px; border-radius: 50%; background: var(--green); box-shadow: 0 0 10px var(--green); display: inline-block; animation: pulse 1.6s infinite; }
  @keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.35; } 100% { opacity: 1; } }
  .meta-bar { font-size: 12px; color: var(--muted); display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
  .btn-refresh { background: rgba(56,189,248,0.15); color: var(--accent); border: 1px solid rgba(56,189,248,0.4); padding: 6px 12px; border-radius: 8px; font-size: 12px; font-weight: 700; cursor: pointer; }
  .advisor-box { border-radius: 14px; padding: 16px; margin-bottom: 14px; border: 1px solid var(--card-border); }
  .advisor-badge { font-size: 15px; font-weight: 800; margin-bottom: 6px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 6px; }
  .advisor-text { font-size: 13.5px; line-height: 1.45; color: #e2e8f0; }
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 14px; }
  @media (max-width: 720px) { .grid-2 { grid-template-columns: 1fr; } }
  .card { background: rgba(17, 24, 39, 0.9); border: 1px solid var(--card-border); border-radius: 14px; padding: 15px; box-shadow: 0 8px 24px rgba(0,0,0,0.3); }
  .card-title { font-size: 12px; text-transform: uppercase; letter-spacing: 0.8px; color: var(--muted); font-weight: 700; margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center; }
  .stat-row { display: flex; justify-content: space-between; gap: 10px; font-size: 13px; padding: 6px 0; border-bottom: 1px solid rgba(255,255,255,0.05); }
  .stat-row:last-child { border-bottom: none; }
  .val-bold { font-weight: 700; text-align: right; }
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
  .btn-danger { background: rgba(239,68,68,0.2); color: #f87171; border: 1px solid rgba(239,68,68,0.6); padding: 8px 14px; border-radius: 8px; font-size: 13px; font-weight: 800; cursor: pointer; }
  .inp { background:#0b1220; color:#f8fafc; border:1px solid var(--card-border); border-radius:8px; padding:7px 10px; font-size:13px; width:170px; }
  .flash-up { color: #34d399 !important; transition: color 0.2s; }
  .flash-dn { color: #f87171 !important; transition: color 0.2s; }
</style>
</head>
<body>

<div class="topbar">
  <div class="brand"><span class="live-dot" id="ws-dot"></span><span>PIONEX AI FAST-SCALP CONSOLE v8.0</span></div>
  <div class="meta-bar">
    <span id="ws-status">Connessione Kraken Live...</span><span>|</span>
    <span id="session-label"></span>
    <button class="btn-refresh" onclick="forcePollKraken()">🔄 Aggiorna Prezzi Live</button>
  </div>
</div>

<div class="advisor-box" id="advisor-box">
  <div class="advisor-badge"><span id="advisor-badge-text"></span><span style="font-size:11px; font-weight:600; opacity:0.85;" id="snapshot-time"></span></div>
  <div class="advisor-text" id="advisor-instruction"></div>
</div>

<div class="card" style="margin-bottom:14px; border-color:#ef4444;">
  <div class="card-title"><span>🚨 CONTROLLO MANUALE / EMERGENZA</span><span id="cmd-status" class="pill pill-amber">Token non impostato</span></div>
  <div style="font-size:12.5px; color:var(--muted); margin-bottom:10px;">I pulsanti avviano il bot su GitHub (esecuzione in 30-90 secondi, conferma su Telegram). Le chiavi Pionex restano nei Secrets e non passano mai dal browser.</div>
  <div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px;">
    <button class="btn-danger" onclick="cmdClose()">🛑 CHIUDI POSIZIONE ORA</button>
    <button class="btn-refresh" onclick="cmdSellAll()">💥 VENDI TUTTO UNA COIN</button>
    <button class="btn-refresh" onclick="setToken()">🔑 Imposta token GitHub</button>
    <a class="btn-refresh" id="actions-link" target="_blank" rel="noopener" style="text-decoration:none;">⚙️ GitHub Actions</a>
    <a class="btn-refresh" href="https://www.pionex.com/" target="_blank" rel="noopener" style="text-decoration:none;">📱 Pionex (chiusura a mano)</a>
  </div>
  <div id="cmd-log" style="font-size:12.5px; color:#e2e8f0;"></div>
</div>

<div class="grid-2">
  <div class="card">
    <div class="card-title"><span>📌 POSIZIONE APERTA IN TEMPO REALE</span><span id="pos-status-pill" class="pill"></span></div>
    <div id="open-pos-content"></div>
  </div>
  <div class="card">
    <div class="card-title"><span>📊 STATISTICHE & RISCHIO PORTAFOGLIO</span><span id="fng-badge" class="pill pill-amber"></span></div>
    <div id="portfolio-stats-content"></div>
  </div>
</div>

<div class="card" style="margin-bottom:14px;">
  <div class="card-title"><span>📡 SCANNER LIVE 10 CRIPTOVALUTE (CLICCA UNA RIGA PER I DETTAGLI)</span><span style="color:var(--accent);">Prezzi Live via Kraken WebSocket</span></div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Coin</th><th>Prezzo Live</th><th>Score</th><th>AI Prob</th><th>Timer 15m</th><th>R:R & Risk</th><th>Azione Manuale Consigliata</th></tr></thead>
      <tbody id="scanner-tbody"></tbody>
    </table>
  </div>
</div>

<div class="card" style="margin-bottom:14px;" id="detail-card">
  <div class="card-title"><span id="detail-title">🔍 PIANO OPERATIVO DETTAGLIATO</span><span id="detail-pair" style="color:var(--accent);"></span></div>
  <div id="detail-content"></div>
  <div style="margin-top:12px; display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
    <input id="open-amount" class="inp" type="number" min="10" step="1" placeholder="USDT (vuoto = auto)">
    <button id="open-btn" class="btn-refresh" onclick="cmdOpen()">🚀 APRI LONG MANUALE</button>
  </div>
</div>

<div class="card">
  <div class="card-title"><span>📜 STORICO POSIZIONI CHIUSE</span><span id="history-count"></span></div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Data Chiusura</th><th>Coin</th><th>Prezzo Entry</th><th>Prezzo Exit</th><th>PnL Netto</th><th>Motivo Chiusura</th></tr></thead>
      <tbody id="history-tbody"></tbody>
    </table>
  </div>
</div>

<script>
const DATA = __CONSOLE_PAYLOAD__;
const livePrices = {};
const wsMap = {};
DATA.coins.forEach(c => { livePrices[c.code] = c.price; wsMap[c.ws] = c.code; });
let selectedCoinCode = DATA.position.is_open ? DATA.position.coin_code : DATA.coins[0].code;

function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, m => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[m])); }

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
    container.innerHTML = `<div style="padding:18px 4px; text-align:center; color:var(--muted); font-size:13.5px;">Nessuna posizione aperta al momento.<br><span style="color:#e2e8f0; font-weight:600; display:block; margin-top:6px;">Il capitale e' protetto in USDT in attesa di un segnale Fast-Scalp.</span></div>`;
    return;
  }
  const code = pos.coin_code;
  const cur = livePrices[code] || pos.entry_price;
  const entry = pos.entry_price, sl = pos.sl_price, tp = pos.tp_price;
  const oneR = pos.one_r || Math.max(entry - pos.initial_sl, entry * 0.009);
  const grossPct = ((cur - entry) / entry) * 100.0;
  const netPct = grossPct - DATA.limits.fee_rt;
  const pnlUsdt = pos.invested_usdt * (netPct / 100.0);
  const curR = oneR > 0 ? (cur - entry) / oneR : 0.0;
  pill.className = "pill " + (netPct >= 0 ? "pill-green" : "pill-red");
  pill.textContent = "LONG " + code + " (" + pos.mode + ")";
  const totalSpan = Math.max(tp - sl, 1e-8);
  const progressPct = Math.max(0, Math.min(100, ((cur - sl) / totalSpan) * 100));
  const barColor = netPct >= 0 ? "var(--green)" : "var(--red)";
  let manualPosAdvice = "✅ MANTIENI POSIZIONE (In linea con il piano)";
  if (cur <= sl) manualPosAdvice = "🛑 ATTENZIONE: Prezzo sotto lo Stop-Loss! Il bot chiude al prossimo giro: valuta chiusura manuale.";
  else if (cur >= tp) manualPosAdvice = "🎯 TARGET RAGGIUNTO: Prendi profitto!";
  container.innerHTML = `
    <div style="display:flex; justify-content:space-between; align-items:baseline;">
      <div><div style="font-size:12px; color:var(--muted);">PnL Netto Live (${esc(code)})</div>
        <div class="pnl-big" style="color:${barColor};">${netPct >= 0 ? "+" : ""}${netPct.toFixed(2)}% (${pnlUsdt >= 0 ? "+" : ""}${pnlUsdt.toFixed(2)} USDT)</div></div>
      <div style="text-align:right;"><div style="font-size:12px; color:var(--muted);">Multiplo R Live</div>
        <div style="font-size:18px; font-weight:800; color:var(--accent);">${curR >= 0 ? "+" : ""}${curR.toFixed(2)}R</div></div>
    </div>
    <div class="progress-wrap"><div class="progress-bar" style="width:${progressPct.toFixed(1)}%; background:${barColor};"></div></div>
    <div style="display:flex; justify-content:space-between; font-size:11px; color:var(--muted); margin-bottom:10px;">
      <span>🛑 SL: ${fmtPrice(sl)}</span><span>📍 Live: <strong style="color:#fff;">${fmtPrice(cur)}</strong></span><span>🎯 TP: ${fmtPrice(tp)}</span>
    </div>
    <div class="stat-row"><span>Prezzo Ingresso (Aperta il ${esc(pos.opened_at)})</span><span class="val-bold">${fmtPrice(entry)}</span></div>
    <div class="stat-row"><span>Capitale Investito / Quantita'</span><span class="val-bold">${pos.invested_usdt.toFixed(2)} USDT (${pos.amount_coin} ${esc(code)})</span></div>
    <div class="stat-row"><span>Stato Protezione Trailing</span><span class="val-bold">${pos.tp2_locked ? "🚀 +1.45R Locked" : (pos.tp1_locked ? "🔒 +1R Locked" : (pos.breakeven_locked ? "🛡️ Break-Even Netto" : "Iniziale"))}</span></div>
    <div class="stat-row"><span>Indicazione Operativa</span><span class="val-bold" style="color:var(--accent);">${manualPosAdvice}</span></div>`;
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
    <div class="stat-row"><span>PnL Giornaliero (stop a -${DATA.limits.daily_max_loss}% del capitale)</span><span class="val-bold" style="color:${dailyColor};">${(s.daily_pnl_pct || 0) >= 0 ? "+" : ""}${(s.daily_pnl_pct || 0).toFixed(2)}% (${s.daily_trades || 0}/${DATA.limits.max_trades} trade oggi)</span></div>
    <div class="stat-row"><span>Ultima Azione Bot</span><span class="val-bold" style="font-size:12px;">${esc(s.last_action)}</span></div>`;
}

function renderScannerTable() {
  const tbody = document.getElementById("scanner-tbody");
  tbody.innerHTML = "";
  DATA.coins.forEach(c => {
    const cur = livePrices[c.code] || c.price;
    const scoreClass = c.final_score >= 7 ? "pill-green" : (c.final_score >= 6 ? "pill-amber" : "pill-red");
    const timerBadge = c.timer_ok ? `<span class="pill pill-green">⚡ OK (${c.timer_triggers}/4)</span>` : `<span class="pill pill-amber">⏳ ${c.timer_triggers}/4</span>`;
    const riskBadge = c.risk_ok ? `<span class="pill pill-green">${c.rr_ratio}x PASS</span>` : `<span class="pill pill-red">${c.rr_ratio}x STOP</span>`;
    let manualAdvice = "🛡️ Attendi (Non aprire)";
    let adviceColor = "var(--muted)";
    if (DATA.position.is_open && DATA.position.coin_code === c.code) { manualAdvice = "📌 POSIZIONE ATTIVA"; adviceColor = "var(--accent)"; }
    else if (c.entry_ok) { manualAdvice = "🚀 APRI LONG ORA"; adviceColor = "var(--green)"; }
    else if (c.raw_100 >= 54) { manualAdvice = "👀 Quasi pronto (Attendi)"; adviceColor = "var(--amber)"; }
    const tr = document.createElement("tr");
    tr.className = "clickable-row";
    if (c.code === selectedCoinCode) tr.style.background = "rgba(56,189,248,0.12)";
    tr.onclick = () => { selectedCoinCode = c.code; renderScannerTable(); renderCoinDetail(); };
    tr.innerHTML = `
      <td><strong>${esc(c.code)}</strong> <span style="color:var(--muted); font-size:11px;">${esc(c.name)}</span></td>
      <td id="price-cell-${c.code}" class="val-bold">${fmtPrice(cur)}</td>
      <td><span class="pill ${scoreClass}">${c.final_score}/10 (${c.raw_100})</span></td>
      <td class="val-bold">${c.ai_prob}%</td><td>${timerBadge}</td><td>${riskBadge}</td>
      <td style="font-weight:700; color:${adviceColor};">${manualAdvice}</td>`;
    tbody.appendChild(tr);
  });
}

function renderCoinDetail() {
  const c = DATA.coins.find(x => x.code === selectedCoinCode) || DATA.coins[0];
  const cur = livePrices[c.code] || c.price;
  document.getElementById("detail-title").textContent = `🔍 PIANO OPERATIVO DETTAGLIATO: ${c.name.toUpperCase()} (${c.code})`;
  document.getElementById("detail-pair").textContent = `Pionex Spot: ${c.pionex}`;
  document.getElementById("open-btn").textContent = `🚀 APRI LONG MANUALE ${c.code}`;
  document.getElementById("detail-content").innerHTML = `
    <div class="grid-2" style="margin-bottom:0;">
      <div>
        <div class="stat-row"><span>Prezzo Live / VWAP 15m</span><span class="val-bold">${fmtPrice(cur)} / ${fmtPrice(c.vwap_15m)}</span></div>
        <div class="stat-row"><span>Budget a rischio fisso</span><span class="val-bold">${c.pos_usdt.toFixed(2)} USDT</span></div>
        <div class="stat-row"><span>Stop-Loss Iniziale</span><span class="val-bold" style="color:var(--red);">${fmtPrice(c.sl_price)}</span></div>
        <div class="stat-row"><span>Take-Profit Target</span><span class="val-bold" style="color:var(--green);">${fmtPrice(c.tp_price)}</span></div>
        <div class="stat-row"><span>Livelli Profit-Lock (BE / +1R / +1.45R)</span><span class="val-bold">${fmtPrice(c.be_trigger)} / ${fmtPrice(c.tp1_trigger)} / ${fmtPrice(c.tp2_trigger)}</span></div>
      </div>
      <div>
        <div class="stat-row"><span>Reti Neurali (MLP / k-NN / MonteCarlo)</span><span class="val-bold">${c.prob_mlp}% / ${c.prob_knn}% / ${c.prob_mc}%</span></div>
        <div class="stat-row"><span>Time Machine (20 pattern simili)</span><span class="val-bold">WinRate ${c.tm_wr}% (${c.tm_exp >= 0 ? "+" : ""}${c.tm_exp}R)</span></div>
        <div class="stat-row"><span>RSI 1H / RSI 15m / Orderbook L2</span><span class="val-bold">${c.rsi_1h} / ${c.rsi_15m} / ${c.ob_imb >= 0 ? "+" : ""}${c.ob_imb}%</span></div>
        <div class="stat-row"><span>Dettaglio Entry Timer 15m</span><span class="val-bold">${esc(c.timer_summary)}</span></div>
        <div class="stat-row"><span>Stato Risk Manager</span><span class="val-bold">${esc(c.risk_status)}</span></div>
      </div>
    </div>`;
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
    return `<tr><td>${esc(h.closed_at)}</td>
      <td><strong>${esc(h.coin)}</strong> <span style="font-size:10px; color:var(--muted);">(${esc(h.mode || "SIM")})</span></td>
      <td>${fmtPrice(h.entry)}</td><td>${fmtPrice(h.exit)}</td>
      <td><span class="pill ${cls}">${sign}${Number(h.pnl_pct).toFixed(2)}% (${sign}${Number(h.pnl_usdt || 0).toFixed(2)} USDT)</span></td>
      <td>${esc(h.reason)}</td></tr>`;
  }).join("");
}

function updateLivePrice(code, newPrice) {
  if (!newPrice || newPrice <= 0) return;
  const oldPrice = livePrices[code] || newPrice;
  livePrices[code] = newPrice;
  const cell = document.getElementById("price-cell-" + code);
  if (cell) {
    cell.textContent = fmtPrice(newPrice);
    if (newPrice > oldPrice) { cell.classList.add("flash-up"); setTimeout(() => cell.classList.remove("flash-up"), 350); }
    else if (newPrice < oldPrice) { cell.classList.add("flash-dn"); setTimeout(() => cell.classList.remove("flash-dn"), 350); }
  }
  if (DATA.position.is_open && DATA.position.coin_code === code) renderOpenPosition();
  if (selectedCoinCode === code) renderCoinDetail();
}

function connectKrakenWebSocket() {
  try {
    const ws = new WebSocket("wss://ws.kraken.com");
    ws.onopen = () => {
      document.getElementById("ws-status").textContent = "🟢 Streaming Live Kraken Attivo";
      ws.send(JSON.stringify({ event: "subscribe", pair: DATA.coins.map(c => c.ws), subscription: { name: "ticker" } }));
    };
    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      if (Array.isArray(msg) && msg.length >= 4 && msg[1] && msg[1].c) {
        const code = wsMap[msg[3]];
        const price = parseFloat(msg[1].c[0]);
        if (code && price > 0) updateLivePrice(code, price);
      }
    };
    ws.onerror = () => { document.getElementById("ws-status").textContent = "🟡 Fallback Polling REST Live"; };
    ws.onclose = () => { setTimeout(connectKrakenWebSocket, 5000); };
  } catch (e) { console.warn("WS error:", e); }
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
          if (k.includes(c.code) || (c.code === "BTC" && k.includes("XBT")) || (c.code === "DOGE" && k.includes("XDG"))) updateLivePrice(c.code, p);
        });
      });
    }
  } catch (e) { console.warn("Poll error:", e); }
}


function tokenNow() {
  try { const t = localStorage.getItem("gh_token"); if (t) return t; } catch (e) {}
  return window._tok || "";
}
function updateTokenPill() {
  const el = document.getElementById("cmd-status");
  const ok = !!tokenNow();
  el.className = "pill " + (ok ? "pill-green" : "pill-amber");
  el.textContent = ok ? "Token impostato" : "Token non impostato";
}
function setToken() {
  const t = prompt("Incolla il token GitHub (fine-grained, SOLO il repo del bot, permesso Actions: Read and write):");
  if (!t) return;
  window._tok = t.trim();
  try { localStorage.setItem("gh_token", t.trim()); } catch (e) {}
  updateTokenPill();
}
async function dispatchBot(command, coin, amount) {
  const log = document.getElementById("cmd-log");
  if (!tokenNow()) { setToken(); }
  const tok = tokenNow();
  if (!tok) { log.textContent = "❌ Token mancante."; return; }
  if (!DATA.repo) { log.textContent = "❌ Repo sconosciuta: usa il pulsante GitHub Actions (Run workflow)."; return; }
  log.textContent = "⏳ Invio comando...";
  try {
    const r = await fetch("https://api.github.com/repos/" + DATA.repo + "/actions/workflows/" + DATA.workflow + "/dispatches", {
      method: "POST",
      headers: { "Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json" },
      body: JSON.stringify({ ref: DATA.ref, inputs: { command: command, coin: coin || "", amount_usdt: String(amount || "") } })
    });
    if (r.status === 204) log.textContent = "✅ Comando '" + command + "' inviato. Conferma su Telegram entro 30-90 secondi.";
    else { const t = await r.text(); log.textContent = "❌ Errore " + r.status + ": " + t.slice(0, 200); }
  } catch (e) { log.textContent = "❌ Errore di rete: " + e; }
}
function cmdClose() {
  const code = DATA.position.is_open ? DATA.position.coin_code : "";
  if (!DATA.position.is_open && !confirm("Il bot non risulta avere posizioni aperte. Inviare comunque il comando?")) return;
  if (!confirm("CHIUDERE a mercato la posizione " + code + " ORA?")) return;
  dispatchBot("close", code, "");
}
function cmdSellAll() {
  const def = DATA.position.is_open ? DATA.position.coin_code : "SOL";
  const c = (prompt("Coin da vendere INTERAMENTE a mercato (es. SOL):", def) || "").toUpperCase().trim();
  if (!c) return;
  if (!confirm("VENDERE TUTTO il saldo libero di " + c + " a mercato?")) return;
  dispatchBot("sell_all", c, "");
}
function cmdOpen() {
  if (DATA.position.is_open) { alert("C'e' gia' una posizione aperta su " + DATA.position.coin_code + ": chiudila prima."); return; }
  const amt = document.getElementById("open-amount").value;
  if (!confirm("APRIRE LONG a mercato su " + selectedCoinCode + (amt ? " con " + amt + " USDT" : " (importo automatico)") + "?")) return;
  dispatchBot("open", selectedCoinCode, amt);
}
document.getElementById("actions-link").href = "https://github.com/" + DATA.repo + "/actions/workflows/" + DATA.workflow;
updateTokenPill();

renderAdvisor(); renderOpenPosition(); renderPortfolioStats(); renderScannerTable(); renderCoinDetail(); renderHistoryTable();
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
        log("ERROR", f"Scrittura {WEBAPP_FILE} fallita: {e}")
        return False


# ==============================================================================
# 11. TELEGRAM (solo apertura / chiusura / errori / run manuale)
# ==============================================================================
def build_telegram_report(ranked_evals, state, trade_status, session_str):
    focus = next((e for e in ranked_evals if entry_conditions(e)), ranked_evals[0])
    advisor = build_manual_action_advisor(state, focus, {e["code"]: e for e in ranked_evals}, trade_status)
    pos, stats = state["position"], state["stats"]
    esc = html_lib.escape

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    rank_lines = []
    for idx, ev in enumerate(ranked_evals[:5]):
        t_icon = "⚡" if ev["scanner_timer"]["triggered"] else "⏳"
        rank_lines.append(f"{medals[idx]} <b>{ev['code']}</b> ({fmt_price(ev['price'])}): <b>{ev['final_score']}/10</b> ({ev['raw_100']}/100) | AI {ev['ai_prob'] * 100:.0f}% | 15m {t_icon}")

    if pos["is_open"]:
        pos_block = (f"📌 <b>POSIZIONE APERTA SU {pos['coin_code']} ({pos['mode']}):</b>\n"
                     f"• Entry: <b>{fmt_price(pos['entry_price'])}</b> | Investito: <b>{pos['invested_usdt']:.2f} USDT</b>\n"
                     f"• Stop-Loss Attivo: <b>{fmt_price(pos['sl_price'])}</b> | Take-Profit: <b>{fmt_price(pos['tp_price'])}</b>\n"
                     f"• PnL Netto Live: <b>{trade_status['pnl_pct']:+.2f}% ({trade_status['pnl_usdt']:+.2f} USDT / {trade_status['current_r']:+.2f}R)</b>")
    else:
        pos_block = "📌 <b>POSIZIONE APERTA:</b> Nessuna (100% Liquidita' in USDT)"

    wr = (stats["wins"] / stats["total_trades"] * 100.0) if stats["total_trades"] > 0 else 0.0
    hist = state.get("history", [])
    if hist:
        h = hist[0]
        closed_block = f"📜 <b>ULTIMA POSIZIONE CHIUSA:</b> {h['coin']} il {h['closed_at']} -> <b>{h['pnl_pct']:+.2f}% ({h.get('pnl_usdt', 0.0):+.2f} USDT)</b> ({esc(h['reason'])})"
    else:
        closed_block = "📜 <b>ULTIMA POSIZIONE CHIUSA:</b> Nessuna operazione chiusa di recente"

    alerts_block = ("\n" + "\n".join(esc(a) for a in trade_status["alerts_fired"]) + "\n") if trade_status.get("alerts_fired") else ""

    return f"""🤖 <b>PIONEX FAST-SCALP AI v8.0</b>
🕒 <i>{session_str}</i>
━━━━━━━━━━━━━━━━━━━━
<b>{esc(trade_status['headline'])}</b>
{esc(trade_status['detail'])}{alerts_block}

🧭 <b>COSA FARE MANUALMENTE ADESSO:</b>
<b>{esc(advisor['badge'])}</b>
<i>{esc(advisor['instruction'])}</i>

{pos_block}
{closed_block}
• Bilancio Storico: <b>{stats['total_trades']} Trade</b> (Win Rate <b>{wr:.0f}%</b> | PnL Netto: <b>{stats['total_pnl_pct']:+.2f}% / {stats['total_pnl_usdt']:+.2f} USDT</b>)

🏆 <b>TOP 5 CLASSIFICA FAST-SCALP (SU 10 COIN):</b>
{chr(10).join(rank_lines)}
━━━━━━━━━━━━━━━━━━━━
📲 <i>Apri il file <b>Console_Live_AI_Bot.html</b> qui sotto per lo streaming live dei prezzi!</i>"""


def send_telegram(msg, attach_console=True):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("WARN", "Credenziali Telegram mancanti nei Secrets.")
        return
    url_msg = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = SESSION.post(url_msg, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML",
                                        "disable_web_page_preview": True}, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            SESSION.post(url_msg, json={"chat_id": TELEGRAM_CHAT_ID, "text": re.sub(r"<[^>]+>", "", msg)}, timeout=HTTP_TIMEOUT)
    except Exception as e:
        log("ERROR", f"Invio messaggio Telegram fallito: {e}")  # redact() oscura il token nell'URL
    if attach_console and os.path.exists(WEBAPP_FILE):
        try:
            with open(WEBAPP_FILE, "rb") as f:
                SESSION.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
                             data={"chat_id": TELEGRAM_CHAT_ID, "caption": "🖥️ Console Web Interattiva (prezzi live, posizioni, guida manuale)"},
                             files={"document": ("Console_Live_AI_Bot.html", f, "text/html")}, timeout=HTTP_TIMEOUT)
        except Exception as e:
            log("ERROR", f"Invio Console HTML fallito: {e}")


# ==============================================================================
# MAIN
# ==============================================================================
def main():
    mode = "LIVE (ordini reali)" if pionex_live_enabled() else "SIMULAZIONE (nessun ordine reale)"
    log("INFO", f"Avvio Pionex Fast-Scalp AI v8.0 | Modalita': {mode}")
    state = load_state()
    pos = state["position"]
    now_utc = datetime.now(timezone.utc)
    in_window = ENTRY_START_UTC <= now_utc.hour < ENTRY_END_UTC
    is_manual_run = GITHUB_EVENT_NAME == "workflow_dispatch"

    # Fuori finestra e senza posizione: niente da fare (risparmia minuti Actions e chiamate API)
    if not pos["is_open"] and not in_window and not is_manual_run:
        log("INFO", f"Fuori finestra operativa ({ENTRY_START_UTC:02d}-{ENTRY_END_UTC:02d} UTC) e nessuna posizione: skip.")
        return

    LIVE_PX.update(fetch_pionex_prices())
    eur_usd = fetch_eur_usd_rate()
    fng_val, fng_class = fetch_fear_and_greed()
    macro_events = fetch_macro_calendar()
    futures_map = fetch_all_kraken_futures()
    session_str = skill_nyse_bell_and_session()

    # Chiusura d'emergenza: analizza solo la coin interessata (esecuzione piu' veloce)
    if MANUAL_COMMAND in ("close", "sell_all"):
        tgt = pos["coin_code"] if (MANUAL_COMMAND == "close" or not MANUAL_COIN) else MANUAL_COIN
        scan_list = [COIN_MAP.get(tgt, COINS[0])]
    # Con posizione aperta e fuori finestra analizza solo la coin aperta (guardia notturna leggera)
    elif pos["is_open"] and not (in_window or is_manual_run):
        scan_list = [COIN_MAP.get(pos["coin_code"], COINS[0])]
    else:
        scan_list = COINS
    all_evals = scan_all_coins(scan_list, futures_map, fng_val, macro_events)

    if pos["is_open"]:
        open_code = pos.get("coin_code", "SOL")
        if open_code not in all_evals:
            log("WARN", f"Analisi di {open_code} non disponibile: uso valutazione neutra col prezzo Pionex")
            all_evals[open_code] = make_fallback_eval(COIN_MAP.get(open_code, COINS[0]), pos)
    if MANUAL_COMMAND == "sell_all" and MANUAL_COIN in COIN_MAP and MANUAL_COIN not in all_evals:
        all_evals[MANUAL_COIN] = make_fallback_eval(COIN_MAP[MANUAL_COIN], {})
    if not all_evals:
        log("ERROR", "Nessuna coin analizzata con successo.")
        return

    ranked_evals = sorted(all_evals.values(), key=lambda x: (x["raw_100"], x["scanner_timer"]["triggered"], x["ai_prob"]), reverse=True)
    for ev in ranked_evals:
        ev["risk_manager"] = skill_budget_and_risk_manager(INVEST_EUR, eur_usd, ev["price"], ev["sl_price"], ev["tp_price"], state, ev["high_soon"], ev["code"])
        ev["exit_plan"] = skill_exit_planner(ev["price"], ev["sl_price"], ev["tp_price"])

    if MANUAL_COMMAND in ("close", "sell_all", "open"):
        log("INFO", f"COMANDO MANUALE: {MANUAL_COMMAND} {MANUAL_COIN} {MANUAL_USDT or ''}")
        trade_status = run_manual_command(state, all_evals, eur_usd, MANUAL_COMMAND, MANUAL_COIN, MANUAL_USDT)
    else:
        trade_status = manage_autonomous_trading(state, ranked_evals, all_evals, eur_usd)
    generate_webapp_html(ranked_evals, state, trade_status, fng_val, fng_class, session_str, eur_usd)

    action_state = trade_status["action_state"]
    notify_states = ("OPENED_NOW", "CLOSED_NOW", "CLOSE_FAILED", "ENTRY_FAILED", "MANUAL_DONE", "MANUAL_NOOP")
    if action_state in notify_states or is_manual_run:
        log("INFO", f"Invio notifica Telegram (action_state={action_state}, manual_run={is_manual_run})")
        send_telegram(build_telegram_report(ranked_evals, state, trade_status, session_str))
    else:
        best = ranked_evals[0]
        log("INFO", f"Giro completato ({action_state} | Migliore: {best['code']} {best['raw_100']}/100). Nessun messaggio Telegram.")
    log("INFO", "Esecuzione completata.")

    if action_state in ("CLOSE_FAILED", "ENTRY_FAILED"):
        raise SystemExit(1)  # il job risulta rosso su GitHub: errore ben visibile (lo stato viene comunque committato)


if __name__ == "__main__":
    main()
