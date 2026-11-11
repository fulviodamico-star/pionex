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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Rilevamento automatico dello stack Freqtrade / FreqAI (Pandas + Scikit-Learn)
try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    pd = None
    HAS_PANDAS = False

try:
    from sklearn.ensemble import HistGradientBoostingClassifier
    import numpy as np
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False


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
DAILY_MAX_LOSS_PCT = float(_env("DAILY_MAX_LOSS_PCT", 4.0))   # % del CAPITALE (MaxDrawdown protection)
MAX_TRADES_PER_DAY = int(_env("MAX_TRADES_PER_DAY", 5))
MAX_CONSEC_LOSSES = int(_env("MAX_CONSEC_LOSSES", 3))         # StoplossGuard protection
MIN_REWARD_RISK_R = 1.15          # R:R NETTO di commissioni (~1.25 lordo)
CASH_BUFFER_PCT = 5.0
MIN_ORDER_USDT = float(_env("MIN_ORDER_USDT", 10.5))
MAX_SPREAD_BPS = float(_env("MAX_SPREAD_BPS", 22.0))          # max spread Bid-Ask consentito (0.22%)
FEE_PCT_PER_SIDE = float(_env("FEE_PCT_PER_SIDE", 0.05))      # fee Pionex (0.05% per lato)
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
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PionexFreqtradeQuantBot/10.0)"}

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
BTC_REGIME = {"state": "NEUTRAL", "ret_1h_pct": 0.0, "rsi_1h": 50.0, "above_ema50": True, "dump_guard": False}

FREQTRADE_CONFIG = {
    "stake_currency": "USDT",
    "dry_run": not bool(PIONEX_API_KEY and PIONEX_API_SECRET and AUTO_TRADE_ENABLED),
    "timeframe": "1h",
    "freqai": {
        "enabled": True,
        "conv_width": 1,
        "train_period_candles": 320,
        "backtest_period_candles": 80,
        "identifier": "pionex_fastscalp_v10",
        "feature_parameters": {
            "include_timeframes": ["15m", "1h", "4h"],
            "indicator_periods_candles": [14, 20, 50],
            "label_period_candles": 4,
            "DI_threshold": 2.35,
            "weight_factor": 0.9,
        },
    },
}


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
            "last_check_ts": 0, "close_fail_count": 0, "mode": "SIMULATION", "order_id": "",
            "enter_tag": "freqai_fastscalp"
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
            "clientOrderId": f"ft{int(time.time())}{random.randint(100, 999)}"}
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
    for _ in range(2):
        time.sleep(2.0)
        bal1 = pionex_get_balances()
        if bal1 is not None:
            base_delta = bal1.get(base, {}).get("free", 0.0) - base0
            spent = bal0.get("USDT", {}).get("free", 0.0) - bal1.get("USDT", {}).get("free", 0.0)
            if base_delta * price >= 0.5 * usdt:
                break

    filled = base_delta * price >= 0.5 * usdt
    if not filled and res["ok"] and bal1 is None:
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
# 4. ACQUISIZIONE DATI (DataProvider Multi-Venue: Pionex L2 + Kraken OHLCV)
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
    """Scarta l'ultima candela ancora aperta (process_only_new_candles=True)."""
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
        out.pop()
    return out


def _compute_ob_metrics(bids, asks, source="PIONEX"):
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
    return {
        "imbalance": (bid_w - ask_w) / total if total > 0 else 0.0,
        "spread_bps": spread_bps,
        "bid_ask_ratio": bid_w / ask_w if ask_w > 0 else 1.0,
        "source": source,
    }


def fetch_orderbook(coin_cfg, count=50):
    """Legge l'Orderbook L2 direttamente da Pionex (venue di esecuzione) con fallback automatico su Kraken."""
    default = {"imbalance": 0.0, "spread_bps": 5.0, "bid_ask_ratio": 1.0, "source": "DEFAULT"}
    # 1. Tentativo nativo Pionex Depth API
    pdata = safe_get_json(f"{PIONEX_BASE}/api/v1/market/depth", params={"symbol": coin_cfg["pionex"], "limit": count}, retries=1)
    try:
        if pdata and pdata.get("result") is not False and "data" in pdata:
            bids = pdata["data"].get("bids", [])
            asks = pdata["data"].get("asks", [])
            if bids and asks:
                return _compute_ob_metrics(bids, asks, source="PIONEX")
    except Exception:
        pass

    # 2. Fallback Kraken Depth API
    kdata = safe_get_json("https://api.kraken.com/0/public/Depth", params={"pair": coin_cfg["kraken"], "count": count})
    if not kdata or kdata.get("error"):
        return default
    try:
        res = kdata.get("result", {})
        key = list(res.keys())[0]
        bids, asks = res[key].get("bids", []), res[key].get("asks", [])
        if bids and asks:
            return _compute_ob_metrics(bids, asks, source="KRAKEN")
    except Exception:
        pass
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


MACRO_STATUS = {"ok": False}


def fetch_macro_calendar():
    now_utc = datetime.now(timezone.utc)
    events = []
    MACRO_STATUS["ok"] = False
    data = safe_get_json("https://nfs.faireconomy.media/ff_calendar_thisweek.json")
    if isinstance(data, list):
        MACRO_STATUS["ok"] = True
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
                if -12.0 <= diff_hours <= 36.0:
                    events.append({"title": title, "impact": impact, "diff_hours": diff_hours, "ts": int(now_utc.timestamp() + diff_hours * 3600)})
            except Exception:
                continue
    if not events:
        try:
            r = SESSION.get("https://nfs.faireconomy.media/ff_calendar_thisweek.xml", timeout=HTTP_TIMEOUT)
            if r.status_code == 200 and r.text.strip().startswith("<"):
                root = ET.fromstring(r.text)
                MACRO_STATUS["ok"] = True
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
                    if -12.0 <= diff_hours <= 36.0:
                        events.append({"title": title, "impact": impact, "diff_hours": diff_hours, "ts": int(now_utc.timestamp() + diff_hours * 3600)})
        except Exception:
            pass
    events.sort(key=lambda x: x["ts"])
    return events


# ==============================================================================
# 5. INDICATORI TECNICI + EMULATORE `ta` (TA-Lib) & `qtpylib` PER FREQTRADE
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
        return [0.0] * len(candles)
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


def calc_bollinger_series(closes, period=20, mult=2.0):
    n = len(closes)
    pct_b_s = [0.5] * n
    bw_s = [0.0] * n
    for i in range(period - 1, n):
        w = closes[i - period + 1:i + 1]
        mid = sum(w) / period
        std = statistics.pstdev(w)
        upper, lower = mid + mult * std, mid - mult * std
        c = closes[i]
        pct_b_s[i] = (c - lower) / (upper - lower) if (upper - lower) > 0 else 0.5
        bw_s[i] = (upper - lower) / mid if mid > 0 else 0.0
    return pct_b_s, bw_s


def calc_adx_series(candles, period=14):
    n = len(candles)
    if n < period * 2 + 2:
        return [20.0] * n, [20.0] * n, [20.0] * n
    tr_list, pdm_list, mdm_list = [0.0], [0.0], [0.0]
    for i in range(1, n):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        ph, pl = candles[i - 1]["high"], candles[i - 1]["low"]
        tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
        up, dn = h - ph, pl - l
        pdm_list.append(up if (up > dn and up > 0) else 0.0)
        mdm_list.append(dn if (dn > up and dn > 0) else 0.0)
    atr_s = calc_ema_series(tr_list, period)
    pdm_s = calc_ema_series(pdm_list, period)
    mdm_s = calc_ema_series(mdm_list, period)
    dx_list, pdi_s, mdi_s = [], [], []
    for a, p, m in zip(atr_s, pdm_s, mdm_s):
        if a <= 0:
            dx_list.append(0.0)
            pdi_s.append(20.0)
            mdi_s.append(20.0)
            continue
        pdi, mdi = 100.0 * (p / a), 100.0 * (m / a)
        pdi_s.append(pdi)
        mdi_s.append(mdi)
        denom = pdi + mdi
        dx_list.append(100.0 * abs(pdi - mdi) / denom if denom > 0 else 0.0)
    adx_s = calc_ema_series(dx_list, period)
    return adx_s, pdi_s, mdi_s


def calc_adx(candles, period=14):
    adx_s, pdi_s, mdi_s = calc_adx_series(candles, period)
    return adx_s[-1], pdi_s[-1], mdi_s[-1]


def calc_supertrend_series(candles, period=10, multiplier=3.0):
    n = len(candles)
    if n < period + 2:
        return [1] * n, [c["close"] for c in candles]
    atr_s = calc_atr_series(candles, period)
    final_upper, final_lower = 0.0, 0.0
    direction = 1
    dirs, vals = [1], [candles[0]["close"]]
    for i in range(1, n):
        hl2 = (candles[i]["high"] + candles[i]["low"]) / 2.0
        atr = atr_s[i]
        basic_upper, basic_lower = hl2 + multiplier * atr, hl2 - multiplier * atr
        prev_close = candles[i - 1]["close"]
        if i == 1:
            final_upper, final_lower = basic_upper, basic_lower
            dirs.append(1)
            vals.append(final_lower)
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
        dirs.append(direction)
        vals.append(final_lower if direction == 1 else final_upper)
    return dirs, vals


def calc_supertrend(candles, period=10, multiplier=3.0):
    dirs, vals = calc_supertrend_series(candles, period, multiplier)
    return (dirs[-1], vals[-1]) if dirs else (1, 0.0)


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


def calc_volume_zscore_series(candles, window=30):
    n = len(candles)
    out = [0.0] * n
    for i in range(window + 1, n):
        vols = [candles[j]["volume"] for j in range(i - window, i)]
        mv, sv = sum(vols) / window, statistics.pstdev(vols)
        out[i] = (candles[i]["volume"] - mv) / sv if sv > 0 else 0.0
    return out


def calc_volume_zscore(candles, window=30):
    if len(candles) < window + 2:
        return 0.0
    vols = [c["volume"] for c in candles[-window - 1:-1]]
    mean_v, std_v = sum(vols) / len(vols), statistics.pstdev(vols)
    return (candles[-1]["volume"] - mean_v) / std_v if std_v > 0 else 0.0


def candles_to_dataframe(candles):
    if HAS_PANDAS:
        df = pd.DataFrame(candles)
        if not df.empty:
            df["date"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df
    return candles


def _df_col(df, col):
    if HAS_PANDAS and isinstance(df, pd.DataFrame):
        return df[col].tolist()
    return [r.get(col, 0.0) for r in df]


def _df_candles(df):
    if HAS_PANDAS and isinstance(df, pd.DataFrame):
        return df[["time", "open", "high", "low", "close", "volume"]].to_dict("records")
    return df


def _to_series(df, values):
    if HAS_PANDAS and isinstance(df, pd.DataFrame):
        return pd.Series(values, index=df.index)
    return values


class _TaLibEmulator:
    """Emula `import talib.abstract as ta` di Freqtrade senza dipendenze C."""

    @staticmethod
    def RSI(df, timeperiod=14):
        return _to_series(df, calc_rsi_series(_df_col(df, "close"), timeperiod))

    @staticmethod
    def EMA(df, timeperiod=20):
        return _to_series(df, calc_ema_series(_df_col(df, "close"), timeperiod))

    @staticmethod
    def SMA(df, timeperiod=20):
        closes = _df_col(df, "close")
        out = [sum(closes[max(0, i - timeperiod + 1):i + 1]) / min(i + 1, timeperiod) for i in range(len(closes))]
        return _to_series(df, out)

    @staticmethod
    def ATR(df, timeperiod=14):
        return _to_series(df, calc_atr_series(_df_candles(df), timeperiod))

    @staticmethod
    def ADX(df, timeperiod=14):
        adx_s, _, _ = calc_adx_series(_df_candles(df), timeperiod)
        return _to_series(df, adx_s)

    @staticmethod
    def PLUS_DI(df, timeperiod=14):
        _, pdi_s, _ = calc_adx_series(_df_candles(df), timeperiod)
        return _to_series(df, pdi_s)

    @staticmethod
    def MINUS_DI(df, timeperiod=14):
        _, _, mdi_s = calc_adx_series(_df_candles(df), timeperiod)
        return _to_series(df, mdi_s)

    @staticmethod
    def MACD(df, fastperiod=12, slowperiod=26, signalperiod=9):
        closes = _df_col(df, "close")
        ema_f = calc_ema_series(closes, fastperiod)
        ema_s = calc_ema_series(closes, slowperiod)
        macd_l = [f - s for f, s in zip(ema_f, ema_s)]
        sig_l = calc_ema_series(macd_l, signalperiod)
        hist_l = [m - s for m, s in zip(macd_l, sig_l)]
        return {
            "macd": _to_series(df, macd_l),
            "macdsignal": _to_series(df, sig_l),
            "macdhist": _to_series(df, hist_l),
        }

    @staticmethod
    def SUPERTREND(df, period=10, multiplier=3.0):
        dirs, vals = calc_supertrend_series(_df_candles(df), period, multiplier)
        return _to_series(df, dirs), _to_series(df, vals)


class _QtpylibEmulator:
    """Emula `import freqtrade.vendor.qtpylib.indicators as qtpylib`."""

    @staticmethod
    def crossed_above(series1, series2):
        if HAS_PANDAS and isinstance(series1, pd.Series):
            s2 = series2 if isinstance(series2, pd.Series) else pd.Series(series2, index=series1.index)
            return (series1 > s2) & (series1.shift(1) <= s2.shift(1))
        s2 = series2 if isinstance(series2, list) else [series2] * len(series1)
        return [False] + [series1[i] > s2[i] and series1[i - 1] <= s2[i - 1] for i in range(1, len(series1))]

    @staticmethod
    def crossed_below(series1, series2):
        if HAS_PANDAS and isinstance(series1, pd.Series):
            s2 = series2 if isinstance(series2, pd.Series) else pd.Series(series2, index=series1.index)
            return (series1 < s2) & (series1.shift(1) >= s2.shift(1))
        s2 = series2 if isinstance(series2, list) else [series2] * len(series1)
        return [False] + [series1[i] < s2[i] and series1[i - 1] >= s2[i - 1] for i in range(1, len(series1))]

    @staticmethod
    def typical_price(df):
        if HAS_PANDAS and isinstance(df, pd.DataFrame):
            return (df["high"] + df["low"] + df["close"]) / 3.0
        return [(r["high"] + r["low"] + r["close"]) / 3.0 for r in df]


ta = _TaLibEmulator()
qtpylib = _QtpylibEmulator()


# ==============================================================================
# 6. FREQAI ENGINE (10-Feature Matrix, Dissimilarity Index, Walk-Forward Ensemble)
# ==============================================================================
def build_feature_matrix(candles_1h):
    """Costruisce 10 feature normalizzate in stile FreqAI (RSI, EMA dist, MACD, Mom, Vol Z, Bar Pos,BB %B, ADX, DI diff, EMA50 dist)."""
    closes = [c["close"] for c in candles_1h]
    rsis = calc_rsi_series(closes, 14)
    ema20s = calc_ema_series(closes, 20)
    ema50s = calc_ema_series(closes, 50)
    macds = calc_macd_hist_series(closes, 12, 26, 9)
    atrs = calc_atr_series(candles_1h, 14)
    pct_bs, _ = calc_bollinger_series(closes, 20, 2.0)
    adxs, pdis, mdis = calc_adx_series(candles_1h, 14)

    features = []
    for i in range(len(candles_1h)):
        if i < 35:
            features.append([0.0] * 10)
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
        f7 = clamp((pct_bs[i] - 0.5) * 2.0, -2.5, 2.5)
        f8 = clamp((adxs[i] - 25.0) / 20.0, -2.0, 2.5)
        f9 = clamp((pdis[i] - mdis[i]) / 30.0, -2.5, 2.5)
        f10 = clamp((c - ema50s[i]) / (3.0 * atr), -2.5, 2.5)
        features.append([f1, f2, f3, f4, f5, f6, f7, f8, f9, f10])
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
    """Ritorna (prob, holdout_acc) con shrinkage verso 0.5 se il modello non batte il baseline sul 25% piu' recente."""
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
    in_dim, hid_dim = len(features[0]), 8
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


def walk_forward_gbdt_predict(candles_1h, features, horizon=4):
    """HistGradientBoostingClassifier con Walk-Forward Validation (75% train / 25% holdout) e shrinkage anti-overfitting."""
    if not HAS_SKLEARN or len(candles_1h) < 140:
        return None, 0.50
    try:
        closes = [c["close"] for c in candles_1h]
        start_idx = max(35, len(candles_1h) - 360)
        end_idx = len(candles_1h) - horizon - 1
        X_all = features[start_idx:end_idx]
        y_all = [1 if closes[i + horizon] > closes[i] else 0 for i in range(start_idx, end_idx)]
        if len(X_all) < 80:
            return None, 0.50

        split = int(len(X_all) * 0.75)
        X_tr, y_tr = np.array(X_all[:split]), np.array(y_all[:split])
        X_ho, y_ho = np.array(X_all[split:]), np.array(y_all[split:])
        if len( set(y_tr) ) < 2:
            return None, 0.50

        clf_val = HistGradientBoostingClassifier(max_depth=3, max_iter=45, min_samples_leaf=15, l2_regularization=1.5, random_state=42)
        clf_val.fit(X_tr, y_tr)
        preds_ho = clf_val.predict(X_ho)
        acc = float(np.mean(preds_ho == y_ho))
        up_rate = float(np.mean(y_ho))
        edge = acc - max(up_rate, 1.0 - up_rate)
        shrink = clamp(edge / 0.06, 0.15, 1.0)

        # Ri-addestra su tutta la finestra walk-forward per la predizione live
        clf_full = HistGradientBoostingClassifier(max_depth=3, max_iter=45, min_samples_leaf=15, l2_regularization=1.5, random_state=42)
        clf_full.fit(np.array(X_all), np.array(y_all))
        raw_prob = float(clf_full.predict_proba(np.array([features[-1]]))[0][1])
        return clamp(0.5 + (raw_prob - 0.5) * shrink, 0.05, 0.95), acc
    except Exception:
        return None, 0.50


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


class FreqAIEngine:
    """
    Motore FreqAI: esegue feature engineering, calcola il Dissimilarity Index (DI_values),
    addestra l'ensemble Walk-Forward (MLP + GradientBoosting + Lorentzian k-NN + Monte Carlo)
    e popola le colonne predittive (`do_predict`, `&-up_prob`).
    """

    def __init__(self, config):
        self.config = config
        self.freqai_cfg = config.get("freqai", {})
        self.fp = self.freqai_cfg.get("feature_parameters", {})
        self.di_threshold = float(self.fp.get("DI_threshold", 2.35))
        self.horizon = int(self.fp.get("label_period_candles", 4))

    def start(self, dataframe, metadata, strategy):
        candles_1h = metadata["candles_1h"]
        for period in self.fp.get("indicator_periods_candles", [14, 20, 50]):
            dataframe = strategy.feature_engineering_expand_all(dataframe, period, metadata=metadata)
        dataframe = strategy.feature_engineering_expand_basic(dataframe, metadata=metadata)
        dataframe = strategy.feature_engineering_standard(dataframe, metadata=metadata)
        dataframe = strategy.set_freqai_targets(dataframe, metadata=metadata)

        features = build_feature_matrix(candles_1h)
        curr_x = features[-1]
        hist_x = features[max(35, len(features) - 160):-1]
        if hist_x:
            dists = [math.sqrt(sum((a - b) ** 2 for a, b in zip(curr_x, hx)) / len(curr_x)) for hx in hist_x]
            di_val = min(dists) if dists else 0.5
        else:
            di_val = 0.5
        do_predict = 1 if di_val <= self.di_threshold else 0

        prob_knn = lorentzian_knn_predict(candles_1h, features, horizon=self.horizon, k=18)
        prob_mlp, mlp_acc = online_mlp_neural_net(candles_1h, features, horizon=self.horizon, epochs=35, lr=0.045)
        prob_gbdt, gbdt_acc = walk_forward_gbdt_predict(candles_1h, features, horizon=self.horizon)

        if prob_gbdt is not None:
            prob_mlp = 0.55 * prob_mlp + 0.45 * prob_gbdt
            mlp_acc = max(mlp_acc, gbdt_acc)

        prob_mc = monte_carlo_Barrier_sim(
            candles_1h, metadata["price"], metadata["tp_price"], metadata["sl_price"], paths=300, horizon_bars=10
        )
        ai_ensemble_prob = 0.40 * prob_mlp + 0.35 * prob_knn + 0.25 * prob_mc
        tm_stats = skill_time_machine_backtest(candles_1h, features, horizon_bars=6)

        if HAS_PANDAS and isinstance(dataframe, pd.DataFrame):
            dataframe["DI_values"] = di_val
            dataframe["do_predict"] = do_predict
            dataframe["&-up_prob"] = ai_ensemble_prob
            dataframe["&-prob_mlp"] = prob_mlp
            dataframe["&-prob_knn"] = prob_knn
            dataframe["&-prob_mc"] = prob_mc
        elif isinstance(dataframe, list) and dataframe:
            dataframe[-1].update({
                "DI_values": di_val, "do_predict": do_predict, "&-up_prob": ai_ensemble_prob,
                "&-prob_mlp": prob_mlp, "&-prob_knn": prob_knn, "&-prob_mc": prob_mc,
            })

        metadata["freqai_result"] = {
            "ai_prob": ai_ensemble_prob, "prob_mlp": prob_mlp, "prob_knn": prob_knn,
            "prob_mc": prob_mc, "mlp_holdout_acc": mlp_acc, "di_values": round(di_val, 2),
            "do_predict": do_predict, "tm_stats": tm_stats,
        }
        return dataframe


# ==============================================================================
# 7. ARCHITETTURA FREQTRADE: `Trade`, `Protections`, `IStrategy`
# ==============================================================================
@dataclass
class Trade:
    """Oggetto `Trade` compatibile con i callback `custom_stoploss` e `custom_exit` di Freqtrade."""
    pair: str
    coin_code: str
    open_rate: float
    amount: float
    stake_amount: float
    open_date_utc: datetime
    stop_loss: float
    initial_stop_loss: float
    take_profit: float
    one_r: float
    max_rate: float
    enter_tag: str = "freqai_fastscalp"
    is_short: bool = False
    breakeven_locked: bool = False
    tp1_locked: bool = False
    tp2_locked: bool = False
    alerts_triggered: list = field(default_factory=list)

    def calc_profit_ratio(self, rate: float) -> float:
        if self.open_rate <= 0:
            return 0.0
        gross = (rate - self.open_rate) / self.open_rate
        return gross - (PIONEX_FEE_ROUNDTRIP_PCT / 100.0)


class IStrategy:
    """Classe base compatibile con `freqtrade.strategy.IStrategy` (INTERFACE_VERSION = 3)."""
    INTERFACE_VERSION: int = 3
    timeframe: str = "1h"
    informative_timeframe: str = "4h"
    detail_timeframe: str = "15m"

    minimal_roi: dict = {
        "0": 0.025,
        "270": 0.0035,
    }
    stoploss: float = -0.020
    trailing_stop: bool = True
    use_custom_stoploss: bool = True
    use_exit_signal: bool = True
    process_only_new_candles: bool = True
    startup_candle_count: int = 60

    def __init__(self, config: dict):
        self.config = config
        self.freqai = FreqAIEngine(config)

    def informative_pairs(self):
        return [(c["pionex"], self.informative_timeframe) for c in COINS]

    def feature_engineering_expand_all(self, dataframe, period: int, metadata: dict, **kwargs):
        return dataframe

    def feature_engineering_expand_basic(self, dataframe, metadata: dict, **kwargs):
        return dataframe

    def feature_engineering_standard(self, dataframe, metadata: dict, **kwargs):
        return dataframe

    def set_freqai_targets(self, dataframe, metadata: dict, **kwargs):
        return dataframe

    def populate_indicators(self, dataframe, metadata: dict):
        return dataframe

    def populate_entry_trend(self, dataframe, metadata: dict):
        return dataframe

    def populate_exit_trend(self, dataframe, metadata: dict):
        return dataframe

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime,
                        current_rate: float, current_profit: float, after_fill: bool = False, **kwargs) -> float:
        return self.stoploss

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime,
                    current_rate: float, current_profit: float, **kwargs):
        return None

    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float,
                            proposed_stake: float, min_stake: float, max_stake: float,
                            leverage: float, entry_tag: str, side: str, **kwargs) -> float:
        return proposed_stake

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float,
                            time_in_force: str, current_time: datetime, entry_tag: str,
                            side: str, **kwargs) -> bool:
        return True

    def should_exit_roi(self, trade: Trade, current_time: datetime, current_profit: float) -> bool:
        elapsed_min = max(0.0, (current_time - trade.open_date_utc).total_seconds() / 60.0)
        for min_str, roi_target in sorted(self.minimal_roi.items(), key=lambda x: int(x[0]), reverse=True):
            if elapsed_min >= int(min_str) and current_profit >= roi_target:
                return True
        return False


class PionexFreqAIStrategy(IStrategy):
    """Strategia Fast-Scalp v10.0 con FreqAI Walk-Forward, BTC Regime Filter e Anti-Slippage Guard."""
    INTERFACE_VERSION = 3
    timeframe = "1h"
    minimal_roi = {
        "0": 0.025,
        "270": 0.0035,
    }
    stoploss = -0.020
    trailing_stop = True
    use_custom_stoploss = True

    @property
    def protections(self):
        return [
            {"method": "CooldownPeriod", "stop_duration": MIN_MINUTES_BETWEEN_TRADES},
            {"method": "StoplossGuard", "lookback_period": 1440, "trade_limit": MAX_CONSEC_LOSSES,
             "stop_duration": COOLDOWN_AFTER_LOSS_MIN, "only_per_pair": False},
            {"method": "MaxDrawdown", "lookback_period": 1440, "max_allowed_drawdown": DAILY_MAX_LOSS_PCT / 100.0},
        ]

    # 1. FREQAI FEATURE ENGINEERING
    def feature_engineering_expand_all(self, dataframe, period: int, metadata: dict, **kwargs):
        if HAS_PANDAS and isinstance(dataframe, pd.DataFrame):
            dataframe[f"%-rsi-period_{period}"] = ta.RSI(dataframe, timeperiod=period)
            dataframe[f"%-ema-period_{period}"] = ta.EMA(dataframe, timeperiod=period)
            dataframe[f"%-atr-period_{period}"] = ta.ATR(dataframe, timeperiod=period)
            dataframe[f"%-adx-period_{period}"] = ta.ADX(dataframe, timeperiod=period)
        return dataframe

    def feature_engineering_expand_basic(self, dataframe, metadata: dict, **kwargs):
        if HAS_PANDAS and isinstance(dataframe, pd.DataFrame):
            dataframe["%-pct-change"] = dataframe["close"].pct_change().fillna(0.0)
            dataframe["%-raw_volume"] = dataframe["volume"]
        return dataframe

    def feature_engineering_standard(self, dataframe, metadata: dict, **kwargs):
        if HAS_PANDAS and isinstance(dataframe, pd.DataFrame):
            macd = ta.MACD(dataframe)
            dataframe["%-macdhist"] = macd["macdhist"]
            dataframe["%-vol_zscore"] = calc_volume_zscore_series(metadata["candles_1h"], 30)
            dataframe["%-day_of_week"] = dataframe["date"].dt.dayofweek / 6.0
            dataframe["%-hour_of_day"] = dataframe["date"].dt.hour / 23.0
        return dataframe

    def set_freqai_targets(self, dataframe, metadata: dict, **kwargs):
        horizon = self.freqai.horizon
        if HAS_PANDAS and isinstance(dataframe, pd.DataFrame):
            dataframe["&-s_close"] = (dataframe["close"].shift(-horizon) - dataframe["close"]) / dataframe["close"]
        return dataframe

    # 2. POPULATE INDICATORS
    def populate_indicators(self, dataframe, metadata: dict):
        candles_1h = metadata["candles_1h"]
        candles_4h = metadata["candles_4h"]
        candles_15m = metadata["candles_15m"]
        ob = metadata["ob"]
        fut = metadata["fut"]
        fng_val = metadata["fng_val"]
        macro_events = metadata["macro_events"]
        price = metadata["price"]

        closes_4h = [c["close"] for c in candles_4h]
        closes_1h = [c["close"] for c in candles_1h]
        closes_15m = [c["close"] for c in candles_15m]

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

        metadata["sl_price"] = sl_price
        metadata["tp_price"] = tp_price

        # Esecuzione motore FreqAI
        dataframe = self.freqai.start(dataframe, metadata, self)
        fai = metadata["freqai_result"]
        ai_ensemble_prob = fai["ai_prob"]
        tm_stats = fai["tm_stats"]

        scanner_timer = skill_chart_scanner_entry_timer(candles_15m, vol_z, ob["imbalance"])

        # A. Trend Multi-Timeframe (max 25)
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

        # B. FreqAI + Time Machine (max 25)
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

        # D. Microstruttura Orderbook L2 & POC (max 15)
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

        # E. Derivati / Sentiment / Macro (max 15)
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

        # Check estensione anti-FOMO (distanza da EMA20 1h in multipli di ATR)
        overextended = (price - ema20_1h) > (2.2 * atr_1h) if atr_1h > 0 else False

        if HAS_PANDAS and isinstance(dataframe, pd.DataFrame):
            dataframe["rsi"] = rsi_1h_series
            dataframe["ema20"] = calc_ema_series(closes_1h, 20)
            dataframe["ema50"] = calc_ema_series(closes_1h, 50)
            dataframe["macdhist"] = macd_hist_s
            dataframe["raw_100"] = raw_100
            dataframe["final_score"] = final_score
            dataframe["scanner_triggered"] = 1 if scanner_timer["triggered"] else 0

        metadata["eval"] = {
            "coin": metadata["coin_cfg"], "code": metadata["code"], "price": price,
            "sl_price": sl_price, "tp_price": tp_price, "atr_1h": atr_1h, "atr_pct": atr_pct,
            "ema20_1h": ema20_1h, "ema50_1h": ema50_1h, "overextended": overextended,
            "raw_100": raw_100, "final_score": final_score, "score_trend": score_trend,
            "score_ai": score_ai, "score_mom": score_mom, "score_micro": score_micro,
            "score_macro": score_macro, "ai_prob": ai_ensemble_prob, "prob_mlp": fai["prob_mlp"],
            "prob_knn": fai["prob_knn"], "prob_mc": fai["prob_mc"],
            "mlp_holdout_acc": fai["mlp_holdout_acc"], "di_values": fai["di_values"],
            "do_predict": fai["do_predict"], "rsi_1h": rsi_1h, "rsi_4h": rsi_4h,
            "rsi_15m": rsi_15m, "rsi_15m_prev": rsi_15m_prev, "adx_1h": adx_1h,
            "hurst": hurst, "z_linreg": z_linreg, "poc_price": poc_price, "vol_z": vol_z,
            "ob": ob, "fut": fut, "st_dir_1h": st_dir_1h, "tm_stats": tm_stats,
            "scanner_timer": scanner_timer, "high_soon": bool(high_soon),
            "c15_recent": [{"time": c["time"], "high": c["high"], "low": c["low"]} for c in candles_15m[-24:]],
        }
        return dataframe

    # 3. POPULATE ENTRY TREND
    def populate_entry_trend(self, dataframe, metadata: dict):
        ev = metadata["eval"]
        cond = (
            ev["final_score"] >= Entry_Score_Min
            and ev["raw_100"] >= Entry_Raw_100_Min
            and ev["ai_prob"] >= 0.50
            and ev["scanner_timer"]["triggered"]
            and ev.get("do_predict", 1) == 1
        )
        tag = f"freqai_s{ev['raw_100']}_ai{int(ev['ai_prob'] * 100)}"
        if HAS_PANDAS and isinstance(dataframe, pd.DataFrame):
            dataframe["enter_long"] = 0
            dataframe["enter_tag"] = ""
            if cond and len(dataframe) > 0:
                dataframe.iloc[-1, dataframe.columns.get_loc("enter_long")] = 1
                dataframe.iloc[-1, dataframe.columns.get_loc("enter_tag")] = tag
        ev["enter_long"] = 1 if cond else 0
        ev["enter_tag"] = tag if cond else ""
        return dataframe

    # 4. POPULATE EXIT TREND
    def populate_exit_trend(self, dataframe, metadata: dict):
        ev = metadata["eval"]
        cond = ev["final_score"] <= 3 and ev["ai_prob"] < 0.40
        tag = f"⚠️ USCITA DIFENSIVA AI (Score {ev['code']} sceso a {ev['final_score']}/10)"
        if HAS_PANDAS and isinstance(dataframe, pd.DataFrame):
            dataframe["exit_long"] = 0
            dataframe["exit_tag"] = ""
            if cond and len(dataframe) > 0:
                dataframe.iloc[-1, dataframe.columns.get_loc("exit_long")] = 1
                dataframe.iloc[-1, dataframe.columns.get_loc("exit_tag")] = tag
        ev["exit_long"] = 1 if cond else 0
        ev["exit_tag"] = tag if cond else ""
        return dataframe

    # 5. CONFIRM TRADE ENTRY (Protezione Anti-Slippage e Anti-FOMO prima dell'ordine Market)
    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float,
                            time_in_force: str, current_time: datetime, entry_tag: str,
                            side: str, **kwargs) -> bool:
        ev = kwargs.get("ev", {})
        spread_bps = ev.get("ob", {}).get("spread_bps", 5.0)
        if spread_bps > MAX_SPREAD_BPS:
            log("WARN", f"confirm_trade_entry rifiutato su {pair}: spread troppo alto ({spread_bps:.1f} bps > {MAX_SPREAD_BPS})")
            return False
        if ev.get("overextended", False):
            log("WARN", f"confirm_trade_entry rifiutato su {pair}: prezzo iper-esteso (> 2.2x ATR sopra EMA20)")
            return False
        return True

    # 6. CUSTOM STOPLOSS (Trailing R-Multiple + Protezione Anomalia FreqAI DI_values)
    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime,
                        current_rate: float, current_profit: float, after_fill: bool = False, **kwargs) -> float:
        open_eval = kwargs.get("open_eval", {})
        entry = trade.open_rate
        sl = trade.stop_loss
        one_r = trade.one_r
        current_r = (current_rate - entry) / one_r if one_r > 0 else 0.0
        be_fee_price = round(entry * (1.0 + (PIONEX_FEE_ROUNDTRIP_PCT + 0.05) / 100.0), 6)
        trail_msg = ""

        # Se FreqAI rileva un'anomalia (do_predict == 0) e siamo in profitto, blinda subito a Break-Even
        freqai_anomaly_lock = (open_eval.get("do_predict", 1) == 0 and current_r >= 0.35)

        if not trade.breakeven_locked and (
            current_r >= 0.65
            or (current_rate >= entry * 1.0055 and open_eval.get("rsi_15m", 50.0) >= 66.0)
            or freqai_anomaly_lock
        ):
            if sl < be_fee_price:
                sl = be_fee_price
            trade.breakeven_locked = True
            trade.alerts_triggered.append(f"🔔 Fast-Scalp Lock: Stop-Loss portato a Break-Even Netto ({fmt_price(sl)})")
            trail_msg = "🛡️ Break-Even Netto Attivato"

        if not trade.tp1_locked and current_r >= 1.0:
            new_sl = round(be_fee_price + 0.30 * one_r, 6)
            if new_sl > sl:
                sl = new_sl
            trade.tp1_locked = True
            trade.alerts_triggered.append(f"🔔 Alert +1.0R: Profitto blindato a {fmt_price(sl)}")
            trail_msg = f"🔒 Profitto +1R Blindato ({fmt_price(sl)})"

        if not trade.tp2_locked and current_r >= 1.45:
            new_sl = round(entry + 0.85 * one_r, 6)
            if new_sl > sl:
                sl = new_sl
            trade.tp2_locked = True
            trade.alerts_triggered.append(f"🔔 Alert +1.45R: Trailing Stop alzato a {fmt_price(sl)}")
            trail_msg = f"🚀 Trailing +1.45R ({fmt_price(sl)})"

        if current_r >= 1.90 and "R190" not in trade.alerts_triggered:
            new_sl = round(entry + 1.35 * one_r, 6)
            if new_sl > sl:
                sl = new_sl
            trade.alerts_triggered.append("R190")
            trade.alerts_triggered.append(f"🔔 Alert +1.9R: Super-Trailing a {fmt_price(sl)}")

        trade.stop_loss = sl
        trade.trail_msg = trail_msg
        return sl

    # 7. CUSTOM EXIT
    def custom_exit(self, pair: str, trade: Trade, current_time: datetime,
                    current_rate: float, current_profit: float, **kwargs):
        open_eval = kwargs.get("open_eval", {})
        net_pnl_pct = current_profit * 100.0
        hours_open = max(0.0, (current_time - trade.open_date_utc).total_seconds() / 3600.0)

        rsi_15m = open_eval.get("rsi_15m", 50.0)
        rsi_15m_prev = open_eval.get("rsi_15m_prev", rsi_15m)
        ob_imb = open_eval.get("ob", {}).get("imbalance", 0.0)
        momentum_fading_15m = (
            (rsi_15m_prev >= 61.0 and rsi_15m < rsi_15m_prev - 4.0)
            or (ob_imb <= -0.10 and open_eval.get("ai_prob", 0.5) < 0.50)
            or (trade.max_rate >= trade.open_rate + 1.0 * trade.one_r and current_rate <= trade.max_rate - 0.40 * trade.one_r)
        )

        if net_pnl_pct >= SCALP_MIN_NET_PROFIT_PCT and momentum_fading_15m:
            return f"💰 FAST-SCALP PROFIT TAKE (+{net_pnl_pct:.2f}% netto su rallentamento 15m)"
        if self.should_exit_roi(trade, current_time, current_profit) and hours_open >= TIME_STOP_PROFIT_HOURS:
            return f"⏱️ TIME-STOP SCALP IN PROFITTO (+{net_pnl_pct:.2f}% dopo {hours_open:.1f}h)"
        if hours_open >= TIME_STOP_STALE_HOURS and open_eval.get("ai_prob", 0.5) < 0.46 and net_pnl_pct > -0.60:
            return f"⏱️ ROTAZIONE CAPITALE ({hours_open:.1f}h in stallo, AI={open_eval['ai_prob'] * 100:.0f}%)"
        if open_eval.get("exit_long") == 1 or (open_eval.get("final_score", 5) <= 3 and open_eval.get("ai_prob", 0.5) < 0.40):
            return f"⚠️ USCITA DIFENSIVA AI (Score {trade.coin_code} sceso a {open_eval['final_score']}/10)"
        return None


STRATEGY = PionexFreqAIStrategy(FREQTRADE_CONFIG)


# ==============================================================================
# 8. SKILL OPERATIVE, BTC REGIME GUARD E PROTECTIONS
# ==============================================================================
def update_btc_market_regime(all_evals):
    """Valuta BTC_USDT come informative pair globale: blocca acquisti altcoin durante dump violenti di BTC."""
    btc_ev = all_evals.get("BTC")
    if not btc_ev or btc_ev.get("fallback"):
        return
    p = btc_ev["price"]
    ema50 = btc_ev.get("ema50_1h", p)
    rsi_1h = btc_ev.get("rsi_1h", 50.0)
    rsi_15m = btc_ev.get("rsi_15m", 50.0)
    above_ema50 = p >= ema50 * 0.995
    dump_guard = (not above_ema50 and rsi_1h < 39.0 and rsi_15m < 35.0) or (btc_ev["final_score"] <= 3 and btc_ev["ai_prob"] < 0.42)
    state_lbl = "BEARISH_DUMP" if dump_guard else ("BULLISH" if (above_ema50 and rsi_1h >= 50.0) else "NEUTRAL")
    BTC_REGIME.update({
        "state": state_lbl,
        "rsi_1h": round(rsi_1h, 1),
        "above_ema50": above_ema50,
        "dump_guard": dump_guard,
    })


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


def skill_budget_and_risk_manager(invest_eur, eur_usd, entry_price, sl_price, tp_price, state, macro_high_soon, code=None, ev=None):
    """Wallets + Freqtrade Protections + BTC Regime Guard + Confirm Trade Entry Checks."""
    base_usdt = invest_eur * eur_usd
    # Auto-compounding prudente sui profitti accumulati nello stato (fino a max 1.5x del capitale iniziale)
    compounded_usdt = clamp(base_usdt + max(0.0, state["stats"].get("total_pnl_usdt", 0.0)), base_usdt, base_usdt * 1.5)
    total_usdt = compounded_usdt
    cash_buffer_usdt = total_usdt * (CASH_BUFFER_PCT / 100.0)
    usable_usdt = total_usdt - cash_buffer_usdt

    sl_dist_pct = ((entry_price - sl_price) / entry_price) * 100.0 if entry_price > 0 else 2.0
    tp_dist_pct = ((tp_price - entry_price) / entry_price) * 100.0 if entry_price > 0 else 2.5
    fee_rt = PIONEX_FEE_ROUNDTRIP_PCT
    rr_ratio = (tp_dist_pct - fee_rt) / (sl_dist_pct + fee_rt) if sl_dist_pct > 0 else 0.0

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
    if code and code != "BTC" and BTC_REGIME.get("dump_guard", False):
        reasons.append(f"BTC in Dump Guard (RSI 1h={BTC_REGIME['rsi_1h']})")
    if ev is not None:
        if ev.get("do_predict", 1) == 0:
            reasons.append(f"FreqAI Outlier (DI={ev.get('di_values', 0):.2f} > {STRATEGY.freqai.di_threshold})")
        if ev.get("ob", {}).get("spread_bps", 0.0) > MAX_SPREAD_BPS:
            reasons.append(f"Spread elevato ({ev['ob']['spread_bps']:.1f} bps)")
        if ev.get("overextended", False):
            reasons.append("Prezzo iper-esteso (>2.2x ATR da EMA20)")
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
# 9. VALUTAZIONE SINGOLA COIN TRAMITE `IStrategy` + `FreqAI`
# ==============================================================================
def evaluate_coin(coin_cfg, futures_map, fng_val, macro_events):
    code, pair = coin_cfg["code"], coin_cfg["kraken"]

    candles_1h = drop_unfinished(fetch_kraken_ohlc(pair, interval=60), 3600)
    candles_15m = drop_unfinished(fetch_kraken_ohlc(pair, interval=15), 900)
    candles_4h = build_4h_from_1h(candles_1h)
    ob = fetch_orderbook(coin_cfg, count=50)

    if len(candles_1h) < 60 or len(candles_4h) < 30 or len(candles_15m) < 30:
        return None

    fut = futures_map.get(coin_cfg["futures"], {"funding_8h_pct": 0.005, "open_interest": 0.0, "available": False})
    ref_price = candles_15m[-1]["close"]
    px = LIVE_PX.get(coin_cfg["pionex"])
    price = px if (px and abs(px / ref_price - 1.0) < 0.03) else ref_price

    dataframe = candles_to_dataframe(candles_1h)
    metadata = {
        "pair": coin_cfg["pionex"],
        "code": code,
        "coin_cfg": coin_cfg,
        "price": price,
        "candles_1h": candles_1h,
        "candles_4h": candles_4h,
        "candles_15m": candles_15m,
        "ob": ob,
        "fut": fut,
        "fng_val": fng_val,
        "macro_events": macro_events,
    }

    dataframe = STRATEGY.populate_indicators(dataframe, metadata)
    dataframe = STRATEGY.populate_entry_trend(dataframe, metadata)
    dataframe = STRATEGY.populate_exit_trend(dataframe, metadata)
    return metadata["eval"]


def make_fallback_eval(coin_cfg, pos):
    px = LIVE_PX.get(coin_cfg["pionex"])
    price = px or pos.get("entry_price", 0.0)
    return {
        "coin": coin_cfg, "code": coin_cfg["code"], "price": price,
        "sl_price": pos.get("sl_price", 0.0), "tp_price": pos.get("tp_price", 0.0),
        "atr_1h": 0.0, "atr_pct": 0.0, "raw_100": 0, "final_score": 5, "ai_prob": 0.5,
        "prob_mlp": 0.5, "prob_knn": 0.5, "prob_mc": 0.5, "mlp_holdout_acc": 0.5,
        "di_values": 0.5, "do_predict": 1, "overextended": False,
        "rsi_1h": 50.0, "rsi_4h": 50.0, "rsi_15m": 50.0, "rsi_15m_prev": 50.0,
        "ob": {"imbalance": 0.0, "spread_bps": 0.0, "bid_ask_ratio": 1.0, "source": "FALLBACK"},
        "tm_stats": {"win_rate": 50.0, "expectancy_r": 0.0, "matches": 0},
        "scanner_timer": {"triggered": False, "triggers": 0, "vwap_15m": 0.0, "summary": "Dati non disponibili"},
        "high_soon": False, "c15_recent": [], "stale": not bool(px), "fallback": True,
        "enter_long": 0, "enter_tag": "", "exit_long": 0, "exit_tag": "",
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
# 10. MOTORE AUTONOMO (FreqtradeBot Execution Loop)
# ==============================================================================
def entry_conditions(ev):
    rm = ev.get("risk_manager")
    return bool(
        not ev.get("fallback") and rm and rm["passed"]
        and ev.get("enter_long", 0) == 1
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
    max_rate = max(pos.get("highest_price", entry), exit_price)
    mfe_pct = ((max_rate - entry) / entry) * 100.0 if entry > 0 else 0.0

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
    stats["daily_pnl_pct"] = round(stats.get("daily_pnl_pct", 0.0) + (pnl_usdt / capital_usdt) * 100.0, 2)
    stats["last_close_ts"] = now_ts
    stats.setdefault("coin_last_close", {})[open_code] = now_ts
    stats["last_action"] = f"CHIUSO {open_code} ({net_pnl_pct:+.2f}%) il {now_str}"

    state.setdefault("history", []).insert(0, {
        "coin": open_code, "entry": entry, "exit": exit_price, "pnl_pct": round(net_pnl_pct, 2),
        "pnl_usdt": round(pnl_usdt, 2), "mfe_pct": round(mfe_pct, 2),
        "reason": close_reason, "closed_at": now_str, "mode": pos.get("mode", "SIMULATION")})
    state["history"] = state["history"][:25]
    pos["is_open"] = False
    pos["close_fail_count"] = 0
    save_state(state)
    return {"action_state": "CLOSED_NOW", "headline": f"🔔 POSIZIONE {open_code} CHIUSA AUTOMATICAMENTE",
            "detail": f"{close_reason} | PnL Netto: {net_pnl_pct:+.2f}% ({pnl_usdt:+.2f} USDT) | Picco MFE: +{mfe_pct:.2f}% | {close['msg']}",
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
    alerts_fired = []

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

    now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    trade = Trade(
        pair=coin_cfg["pionex"],
        coin_code=open_code,
        open_rate=entry,
        amount=pos["amount_coin"],
        stake_amount=pos["invested_usdt"],
        open_date_utc=datetime.fromtimestamp(opened_ts, tz=timezone.utc),
        stop_loss=sl,
        initial_stop_loss=pos.get("initial_sl", sl),
        take_profit=tp,
        one_r=one_r,
        max_rate=highest,
        enter_tag=pos.get("enter_tag", "freqai_fastscalp"),
        breakeven_locked=pos.get("breakeven_locked", False),
        tp1_locked=pos.get("tp1_locked", False),
        tp2_locked=pos.get("tp2_locked", False),
        alerts_triggered=list(pos.get("alerts_triggered", [])),
    )

    prev_alerts_len = len(trade.alerts_triggered)
    sl = STRATEGY.custom_stoploss(
        pair=trade.pair, trade=trade, current_time=now_dt,
        current_rate=curr_price, current_profit=net_pnl_pct / 100.0, open_eval=open_eval
    )
    pos["sl_price"] = sl
    pos["breakeven_locked"] = trade.breakeven_locked
    pos["tp1_locked"] = trade.tp1_locked
    pos["tp2_locked"] = trade.tp2_locked
    pos["alerts_triggered"] = [a for a in trade.alerts_triggered if a == "R190"]
    alerts_fired = [a for a in trade.alerts_triggered[prev_alerts_len:] if a != "R190"]
    trail_msg = getattr(trade, "trail_msg", "")

    close_reason = None
    if wick_low <= sl_at_start or curr_price <= sl:
        kind = "PROFIT LOCK / TRAILING STOP" if sl_at_start > entry else "STOP LOSS"
        tag = " (toccato tra due controlli)" if curr_price > sl and wick_low <= sl_at_start else ""
        close_reason = f"{'🔒' if sl_at_start > entry else '🛑'} {kind} ({fmt_price(curr_price)}){tag}"
    elif curr_price >= tp or wick_high >= tp:
        close_reason = f"🎯 TAKE PROFIT RAGGIUNTO ({fmt_price(curr_price)})"
    else:
        close_reason = STRATEGY.custom_exit(
            pair=trade.pair, trade=trade, current_time=now_dt,
            current_rate=curr_price, current_profit=net_pnl_pct / 100.0, open_eval=open_eval
        )

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
    now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    candidates = [
        ev for ev in ranked_evals
        if entry_conditions(ev) and STRATEGY.confirm_trade_entry(
            pair=ev["coin"]["pionex"], order_type="market", amount=ev["risk_manager"]["position_usdt"],
            rate=ev["price"], time_in_force="GTC", current_time=now_dt,
            entry_tag=ev.get("enter_tag", ""), side="long", ev=ev
        )
    ]

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
    k = entry / price if price > 0 else 1.0
    sl_price, tp_price = ev["sl_price"] * k, ev["tp_price"] * k
    one_r = max(entry - sl_price, entry * 0.009)
    pos.update({
        "is_open": True, "coin_code": code, "symbol": coin_cfg["pionex"], "entry_price": entry,
        "amount_coin": buy["amount_coin"], "invested_usdt": buy["invested_usdt"], "sl_price": sl_price,
        "tp_price": tp_price, "initial_sl": sl_price, "one_r": one_r, "highest_price": entry,
        "breakeven_locked": False, "tp1_locked": False, "tp2_locked": False, "alerts_triggered": [],
        "opened_at": now_str, "opened_ts": now_ts, "last_check_ts": now_ts, "close_fail_count": 0,
        "mode": buy["mode"], "order_id": buy.get("order_id", "SIM"),
        "enter_tag": ev.get("enter_tag") or f"freqai_{label.lower()}"})
    stats["daily_trades"] = stats.get("daily_trades", 0) + 1
    stats["last_action"] = f"APERTO LONG {code} a {fmt_price(entry)} il {now_str} ({label})"
    save_state(state)
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
# 11. WEB CONSOLE (index.html)
# ==============================================================================
def build_manual_action_advisor(state, best_eval, all_evals, trade_status):
    pos = state["position"]
    if pos["is_open"]:
        code = pos["coin_code"]
        ev = all_evals.get(code, best_eval)
        pnl, curr_r = trade_status["pnl_pct"], trade_status["current_r"]
        if ev["final_score"] <= 4 or ev["ai_prob"] < 0.44:
            return {"badge": "⚠️ CONSIGLIO MANUALE: VALUTA CHIUSURA ANTICIPATA", "color": "#f59e0b", "bg": "rgba(245,158,11,0.14)",
                    "instruction": f"Il punteggio di {code} si sta indebolendo ({ev['final_score']}/10, FreqAI {ev['ai_prob'] * 100:.1f}%). Se operi manualmente su Pionex, valuta di chiudere o stringere lo Stop-Loss."}
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


def generate_webapp_html(ranked_evals, state, trade_status, fng_val, fng_class, session_str, eur_usd=1.08, macro_events=None):
    focus = next((e for e in ranked_evals if entry_conditions(e)), ranked_evals[0])
    advisor = build_manual_action_advisor(state, focus, {e["code"]: e for e in ranked_evals}, trade_status)
    now = datetime.now(timezone.utc)

    coins_payload = []
    for e in ranked_evals:
        rm = e.get("risk_manager") or skill_budget_and_risk_manager(INVEST_EUR, eur_usd, e["price"], e["sl_price"], e["tp_price"], state, e["high_soon"], e["code"], ev=e)
        coins_payload.append({
            "code": e["code"], "name": e["coin"]["name"], "ws": e["coin"]["ws"], "kraken": e["coin"]["kraken"],
            "price": e["price"], "sl_price": e["sl_price"], "tp_price": e["tp_price"],
            "final_score": e["final_score"], "raw_100": e["raw_100"], "ai_prob": round(e["ai_prob"] * 100),
            "di_val": e.get("di_values", 0.5), "spread_bps": round(e.get("ob", {}).get("spread_bps", 5.0), 1),
            "timer_summary": e["scanner_timer"]["summary"], "risk_ok": rm["passed"], "risk_status": rm["status_str"],
            "rr_ratio": round(rm["rr_ratio"], 2), "pos_usdt": round(rm["position_usdt"], 2), "entry_ok": entry_conditions(e)})

    macro = [{"title": ev["title"], "impact": ev["impact"], "ts": ev["ts"]} for ev in (macro_events or []) if "ts" in ev]
    payload = {
        "updated_ts": int(now.timestamp()), "session": session_str, "fng_val": fng_val, "fng_class": fng_class,
        "btc_regime": BTC_REGIME, "mode": "LIVE" if pionex_live_enabled() else "SIMULAZIONE", "advisor": advisor,
        "waiting": trade_status["detail"] if trade_status.get("action_state") == "WAITING" else "",
        "position": state["position"], "stats": state["stats"], "history": state.get("history", []),
        "coins": coins_payload, "macro": macro, "macro_ok": bool(MACRO_STATUS.get("ok")),
        "repo": os.getenv("GITHUB_REPOSITORY", ""), "workflow": "bot.yml", "ref": os.getenv("GITHUB_REF_NAME") or "main",
        "limits": {"max_trades": MAX_TRADES_PER_DAY, "daily_max_loss": DAILY_MAX_LOSS_PCT, "fee_rt": PIONEX_FEE_ROUNDTRIP_PCT}}
    payload_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    html = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pionex FreqAI Bot v10</title>
<style>
  :root { --bg:#0b1220; --card:#121a2b; --line:#22304a; --text:#f1f5f9; --muted:#8fa0b8; --green:#22c55e; --red:#ef4444; --amber:#f59e0b; --blue:#38bdf8; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); padding: 12px; max-width: 720px; margin: 0 auto; font-size: 15px; line-height: 1.4; }
  h2 { font-size: 13px; letter-spacing: .6px; text-transform: uppercase; color: var(--muted); margin-bottom: 12px; font-weight: 700; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 16px; margin-bottom: 14px; }
  .head { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
  .title { font-size: 19px; font-weight: 800; }
  .chips { display: flex; gap: 6px; flex-wrap: wrap; }
  .chip { display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 700; white-space: nowrap; }
  .g { background: rgba(34,197,94,.15); color: #4ade80; } .r { background: rgba(239,68,68,.15); color: #f87171; }
  .a { background: rgba(245,158,11,.15); color: #fbbf24; } .b { background: rgba(56,189,248,.15); color: #7dd3fc; }
  .n { background: rgba(143,160,184,.15); color: var(--muted); }
  .banner { border-radius: 14px; padding: 12px 14px; margin-bottom: 14px; font-size: 15px; font-weight: 700; }
  .warn { display: none; background: rgba(245,158,11,.15); border: 1px solid var(--amber); color: #fbbf24; border-radius: 12px; padding: 10px 12px; margin-bottom: 14px; font-size: 13.5px; font-weight: 600; }
  .sub { color: var(--muted); font-size: 12.5px; font-weight: 400; }
  .big { font-size: 38px; font-weight: 800; line-height: 1.05; }
  .grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin: 14px 0; }
  .stat { background: #0d1626; border-radius: 12px; padding: 10px 12px; }
  .stat small { display: block; color: var(--muted); font-size: 12px; }
  .stat b { font-size: 16px; }
  .bar { position: relative; height: 12px; border-radius: 8px; background: #1b2740; margin: 16px 0 6px; }
  .fill { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 8px; transition: width .4s; }
  .tick { position: absolute; top: -4px; bottom: -4px; width: 2px; background: #fff; opacity: .85; }
  .scale { display: flex; justify-content: space-between; font-size: 12px; color: var(--muted); }
  .btn { border: 1px solid var(--line); background: #16213a; color: var(--text); padding: 10px 14px; border-radius: 10px; font-size: 14px; font-weight: 700; cursor: pointer; text-decoration: none; display: inline-block; }
  .btn.danger { background: rgba(239,68,68,.18); border-color: rgba(239,68,68,.6); color: #fca5a5; width: 100%; padding: 14px; font-size: 15px; margin-top: 4px; }
  .empty { text-align: center; padding: 14px 4px; }
  .empty b { display: block; font-size: 17px; margin: 6px 0; }
  .sumrow { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 12px; }
  .sumrow div { background: #0d1626; border-radius: 10px; padding: 8px; text-align: center; }
  .sumrow small { display: block; color: var(--muted); font-size: 11px; }
  .sumrow b { font-size: 14px; }
  .h { padding: 11px 0; border-bottom: 1px solid var(--line); }
  .h:last-child { border-bottom: none; }
  .h-top { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
  .h-res { text-align: right; font-weight: 800; }
  .h-res small { display: block; font-weight: 500; font-size: 12px; }
  .why { color: var(--muted); font-size: 12.5px; margin-top: 3px; }
  .ev { display: flex; gap: 12px; align-items: center; padding: 9px 0; border-bottom: 1px solid var(--line); }
  .ev:last-child { border-bottom: none; }
  .evt { font-weight: 800; min-width: 46px; font-size: 15px; }
  .evn { flex: 1; font-size: 14px; }
  .day { font-size: 12px; color: var(--muted); text-transform: uppercase; margin: 12px 0 2px; font-weight: 700; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th { text-align: left; color: var(--muted); font-size: 11px; text-transform: uppercase; padding: 6px 4px; }
  td { padding: 9px 4px; border-top: 1px solid var(--line); }
  tr.row { cursor: pointer; }
  tr.drow { display: none; } tr.drow.open { display: table-row; }
  tr.drow td { color: var(--muted); font-size: 12.5px; background: #0d1626; border-radius: 8px; }
  details summary { cursor: pointer; font-weight: 700; font-size: 14px; }
  .inp { background: #0b1220; color: var(--text); border: 1px solid var(--line); border-radius: 8px; padding: 9px 10px; font-size: 14px; width: 100%; }
  .flex { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
  .flex > * { flex: 1; min-width: 120px; }
  .note { font-size: 12px; color: var(--muted); margin-top: 10px; }
</style>
</head>
<body>

<div class="head">
  <div class="title">🤖 Pionex FreqAI v10</div>
  <div class="chips"><span id="mode" class="chip"></span><span id="btc-chip" class="chip"></span><span id="upd" class="chip"></span></div>
</div>
<div id="stale" class="warn">⚠️ Dati vecchi: il bot non risulta in esecuzione da piu' di 90 minuti. Controlla GitHub Actions.</div>
<div id="banner" class="banner"></div>

<div class="card" id="pos"></div>

<div class="card">
  <h2>📜 Storico posizioni chiuse</h2>
  <div id="sum" class="sumrow"></div>
  <div id="hist"></div>
</div>

<div class="card">
  <h2>📅 News e calendario (cosa valuta il bot)</h2>
  <div id="macro-banner" class="banner" style="margin-bottom:10px;"></div>
  <div id="macro-list"></div>
  <div id="fng" class="sub" style="margin-top:12px;"></div>
  <div class="note">Il bot non legge i titoli dei giornali: considera solo il calendario macro USD (impatto Alto e Medio) e l'indice Fear &amp; Greed. Con un evento ad impatto Alto sospende i nuovi acquisti da 2 ore prima a 30 minuti dopo.</div>
</div>

<div class="card">
  <h2>📡 Mercato: le 10 coin controllate (FreqAI + Pionex L2)</h2>
  <table>
    <thead><tr><th>Coin</th><th>Prezzo</th><th>Punteggio</th><th>Stato</th></tr></thead>
    <tbody id="scan"></tbody>
  </table>
  <div class="note">Tocca una riga per i dettagli (DI_values FreqAI, Spread Pionex in bps, Stop/Target).</div>
</div>

<div class="card">
  <details>
    <summary>🚨 Controlli manuali / emergenza</summary>
    <div class="note" style="margin-top:8px;">I comandi avviano il bot su GitHub (30-90 secondi) e ti arriva conferma su Telegram. Le chiavi Pionex restano nei Secrets.</div>
    <div class="flex">
      <select id="m-coin" class="inp"></select>
      <input id="m-amt" class="inp" type="number" min="10" step="1" placeholder="USDT (vuoto = auto)">
    </div>
    <div class="flex">
      <button class="btn" onclick="cmdOpen()">🚀 Apri long manuale</button>
      <button class="btn" onclick="cmdSellAll()">💥 Vendi tutto il saldo della coin</button>
    </div>
    <div class="flex">
      <button class="btn" onclick="setToken()">🔑 Imposta token GitHub</button>
      <a class="btn" id="actions-link" target="_blank" rel="noopener">⚙️ GitHub Actions</a>
      <a class="btn" href="https://www.pionex.com/" target="_blank" rel="noopener">📱 Pionex</a>
    </div>
    <div class="sub" style="margin-top:10px;">Token: <span id="tok-state"></span></div>
    <div id="cmd-log" style="font-size:13px; margin-top:8px;"></div>
  </details>
</div>

<script>
const DATA = __CONSOLE_PAYLOAD__;
const live = {}; const wsMap = {};
DATA.coins.forEach(c => { live[c.code] = c.price; wsMap[c.ws] = c.code; });
const $ = id => document.getElementById(id);

function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, m => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[m])); }
function fp(p) {
  p = Number(p || 0);
  if (p <= 0) return "$0.00";
  if (p >= 1000) return "$" + p.toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
  if (p >= 10) return "$" + p.toFixed(2);
  if (p >= 1) return "$" + p.toFixed(3);
  return "$" + p.toFixed(4);
}
function sg(x) { return (x >= 0 ? "+" : "") + x.toFixed(2); }
function ago(ts) {
  const m = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60));
  if (m < 60) return m + " min fa";
  return Math.floor(m / 60) + "h " + (m % 60) + "m fa";
}
function dur(ts) {
  const m = Math.max(0, Math.round((Date.now() / 1000 - ts) / 60));
  return m < 60 ? m + " min" : Math.floor(m / 60) + "h " + (m % 60) + "m";
}
function hm(ts) { return new Date(ts * 1000).toLocaleTimeString("it-IT", {hour: "2-digit", minute: "2-digit"}); }
function dayLabel(ts) {
  const d = new Date(ts * 1000), t = new Date(), tm = new Date(t.getTime() + 86400000);
  if (d.toDateString() === t.toDateString()) return "Oggi";
  if (d.toDateString() === tm.toDateString()) return "Domani";
  return d.toLocaleDateString("it-IT", {weekday: "long", day: "numeric", month: "long"});
}

function renderHead() {
  const age = (Date.now() / 1000 - DATA.updated_ts) / 60;
  $("mode").className = "chip " + (DATA.mode === "LIVE" ? "r" : "b");
  $("mode").textContent = DATA.mode === "LIVE" ? "LIVE · soldi reali" : "SIMULAZIONE";
  const br = DATA.btc_regime || {state: "NEUTRAL"};
  $("btc-chip").className = "chip " + (br.state === "BULLISH" ? "g" : (br.state === "BEARISH_DUMP" ? "r" : "n"));
  $("btc-chip").textContent = "BTC: " + br.state;
  $("upd").className = "chip " + (age > 90 ? "a" : "n");
  $("upd").textContent = "Aggiornato " + ago(DATA.updated_ts);
  $("stale").style.display = age > 90 ? "block" : "none";
  const a = DATA.advisor, b = $("banner");
  b.style.background = a.bg;
  b.style.border = "1px solid " + a.color;
  b.innerHTML = `<div style="color:${a.color}">${esc(a.badge)}</div><div class="sub" style="margin-top:4px;color:#cbd5e1;">${esc(a.instruction)}</div>`;
}

function renderPosition() {
  const p = DATA.position, el = $("pos");
  if (!p.is_open) {
    el.innerHTML = `<h2>📌 Posizione aperta</h2><div class="empty"><div style="font-size:30px;">⚪</div><b>Nessuna posizione aperta</b><div class="sub">Il capitale e' in USDT, il bot attende un segnale.</div>${DATA.waiting ? `<div class="sub" style="margin-top:8px;">${esc(DATA.waiting)}</div>` : ""}</div>`;
    return;
  }
  const prot = p.tp2_locked ? "Profitto blindato +1.45R" : (p.tp1_locked ? "Profitto blindato +1R" : (p.breakeven_locked ? "Break-even (non puoi perdere)" : "Stop iniziale"));
  el.innerHTML = `
    <h2>📌 Posizione aperta · ${esc(p.coin_code)} <span class="chip ${p.mode === "LIVE_PIONEX" ? "r" : "b"}">${p.mode === "LIVE_PIONEX" ? "REALE" : "SIM"}</span></h2>
    <div id="pos-pnl" class="big">—</div>
    <div id="pos-usdt" class="sub" style="font-size:14px;"></div>
    <div class="bar"><div id="pos-fill" class="fill"></div><div id="pos-tick" class="tick"></div></div>
    <div class="scale"><span>🛑 Stop ${fp(p.sl_price)}</span><span id="pos-now"></span><span>🎯 Target ${fp(p.tp_price)}</span></div>
    <div class="grid">
      <div class="stat"><small>Prezzo di ingresso</small><b>${fp(p.entry_price)}</b></div>
      <div class="stat"><small>Investito</small><b>${p.invested_usdt.toFixed(2)} USDT</b></div>
      <div class="stat"><small>Aperta da</small><b>${dur(p.opened_ts)}</b></div>
      <div class="stat"><small>Protezione</small><b style="font-size:13px;">${prot}</b></div>
    </div>
    <button class="btn danger" onclick="cmdClose()">🛑 CHIUDI ORA A MERCATO</button>`;
  updatePosLive();
}

function updatePosLive() {
  const p = DATA.position;
  if (!p.is_open || !$("pos-pnl")) return;
  const cur = live[p.coin_code] || p.entry_price, entry = p.entry_price;
  const net = ((cur - entry) / entry) * 100 - DATA.limits.fee_rt;
  const usdt = p.invested_usdt * net / 100;
  const oneR = p.one_r || Math.max(entry - p.initial_sl, entry * 0.009);
  const r = (cur - entry) / oneR;
  const col = net >= 0 ? "var(--green)" : "var(--red)";
  $("pos-pnl").style.color = col;
  $("pos-pnl").textContent = sg(net) + "%";
  $("pos-usdt").textContent = sg(usdt) + " USDT · " + sg(r) + "R (al netto delle commissioni)";
  const span = Math.max(p.tp_price - p.sl_price, 1e-9);
  const pc = x => Math.max(0, Math.min(100, (x - p.sl_price) / span * 100));
  $("pos-fill").style.width = pc(cur) + "%";
  $("pos-fill").style.background = col;
  $("pos-tick").style.left = pc(entry) + "%";
  $("pos-now").textContent = "Ora " + fp(cur);
}

function renderHistory() {
  const s = DATA.stats, h = DATA.history || [];
  const wr = s.total_trades > 0 ? Math.round(s.wins / s.total_trades * 100) + "%" : "—";
  const tc = s.total_pnl_usdt >= 0 ? "var(--green)" : "var(--red)";
  const dc = (s.daily_pnl_usdt || 0) >= 0 ? "var(--green)" : "var(--red)";
  $("sum").innerHTML = `
    <div><small>Chiuse</small><b>${s.total_trades}</b></div>
    <div><small>Vinte</small><b>${wr}</b></div>
    <div><small>Totale</small><b style="color:${tc};">${sg(s.total_pnl_usdt)}$</b></div>
    <div><small>Oggi (${s.daily_trades || 0}/${DATA.limits.max_trades})</small><b style="color:${dc};">${sg(s.daily_pnl_usdt || 0)}$</b></div>`;
  if (h.length === 0) { $("hist").innerHTML = `<div class="sub" style="text-align:center;padding:14px;">Ancora nessuna posizione chiusa.</div>`; return; }
  $("hist").innerHTML = h.map(x => {
    const win = x.pnl_pct >= 0, col = win ? "var(--green)" : "var(--red)";
    return `<div class="h">
      <div class="h-top">
        <div><b>${esc(x.coin)}</b> <span class="chip n">${x.mode === "LIVE_PIONEX" ? "REALE" : "SIM"}</span>
          <div class="sub">${esc(x.closed_at)} · ${fp(x.entry)} → ${fp(x.exit)}</div></div>
        <div class="h-res" style="color:${col};">${sg(x.pnl_pct)}%<small>${sg(x.pnl_usdt || 0)} USDT</small></div>
      </div>
      <div class="why">${esc(x.reason)}</div></div>`;
  }).join("");
}

function renderMacro() {
  const now = Date.now() / 1000, ev = DATA.macro || [];
  let blocking = null, nextHigh = null;
  ev.forEach(e => {
    if (e.impact !== "High") return;
    if (now >= e.ts - 7200 && now <= e.ts + 1800) blocking = e;
    else if (e.ts > now && (!nextHigh || e.ts < nextHigh.ts)) nextHigh = e;
  });
  const b = $("macro-banner");
  if (!DATA.macro_ok) {
    b.style.background = "rgba(245,158,11,.14)"; b.style.border = "1px solid var(--amber)";
    b.innerHTML = `<div style="color:#fbbf24;">⚠️ Calendario non raggiungibile in questo giro</div><div class="sub">Il bot non ha potuto controllare le news: nessun blocco applicato.</div>`;
  } else if (blocking) {
    b.style.background = "rgba(239,68,68,.14)"; b.style.border = "1px solid var(--red)";
    b.innerHTML = `<div style="color:#f87171;">⛔ Acquisti sospesi: ${esc(blocking.title)} alle ${hm(blocking.ts)}</div>`;
  } else if (nextHigh) {
    b.style.background = "rgba(56,189,248,.12)"; b.style.border = "1px solid var(--blue)";
    b.innerHTML = `<div style="color:#7dd3fc;">✅ Nessun blocco ora</div><div class="sub">Prossimo evento forte: ${esc(nextHigh.title)} alle ${hm(nextHigh.ts)} (stop acquisti da 2 ore prima)</div>`;
  } else {
    b.style.background = "rgba(34,197,94,.12)"; b.style.border = "1px solid var(--green)";
    b.innerHTML = `<div style="color:#4ade80;">✅ Nessun evento forte in arrivo</div>`;
  }
  let html = "", last = "";
  ev.filter(e => e.ts > now - 6 * 3600).forEach(e => {
    const d = dayLabel(e.ts);
    if (d !== last) { html += `<div class="day">${esc(d)}</div>`; last = d; }
    let chip;
    if (now > e.ts + 1800) chip = `<span class="chip n">concluso</span>`;
    else if (e.impact === "High" && now >= e.ts - 7200) chip = `<span class="chip r">⛔ blocca acquisti</span>`;
    else if (e.impact === "High") chip = `<span class="chip a">⚠️ stop 2h prima</span>`;
    else chip = `<span class="chip n">monitorato</span>`;
    html += `<div class="ev"><div class="evt">${hm(e.ts)}</div><div class="evn">${esc(e.title)}<div class="sub">Impatto ${e.impact === "High" ? "ALTO" : "medio"} · USD</div></div>${chip}</div>`;
  });
  $("macro-list").innerHTML = html || (DATA.macro_ok ? `<div class="sub">Nessun evento USD importante nelle prossime 36 ore.</div>` : "");
  $("fng").textContent = "Sentiment di mercato: Fear & Greed " + DATA.fng_val + " (" + DATA.fng_class + ")";
}

function renderScanner() {
  const tb = $("scan");
  tb.innerHTML = "";
  DATA.coins.forEach(c => {
    let st = `<span class="chip n">—</span>`;
    if (DATA.position.is_open && DATA.position.coin_code === c.code) st = `<span class="chip b">📌 Aperta</span>`;
    else if (c.entry_ok) st = `<span class="chip g">✅ Pronta</span>`;
    else if (c.raw_100 >= 54) st = `<span class="chip a">👀 Quasi</span>`;
    const tr = document.createElement("tr");
    tr.className = "row";
    tr.onclick = () => $("d-" + c.code).classList.toggle("open");
    tr.innerHTML = `<td><b>${esc(c.code)}</b></td><td id="px-${c.code}">${fp(live[c.code] || c.price)}</td><td>${c.raw_100}/100</td><td>${st}</td>`;
    tb.appendChild(tr);
    const dr = document.createElement("tr");
    dr.className = "drow"; dr.id = "d-" + c.code;
    dr.innerHTML = `<td colspan="4">Stop ${fp(c.sl_price)} · Target ${fp(c.tp_price)} · Budget ${c.pos_usdt.toFixed(2)} USDT · FreqAI ${c.ai_prob}% (DI=${c.di_val}) · Spread ${c.spread_bps} bps<br>Segnale 15m: ${esc(c.timer_summary)}<br>Controllo rischio: ${esc(c.risk_status)}</td>`;
    tb.appendChild(dr);
  });
  const sel = $("m-coin");
  DATA.coins.forEach(c => { const o = document.createElement("option"); o.value = c.code; o.textContent = c.code + " - " + c.name; sel.appendChild(o); });
  if (DATA.position.is_open) sel.value = DATA.position.coin_code;
}

function updateLivePrice(code, price) {
  if (!price || price <= 0) return;
  live[code] = price;
  const c = $("px-" + code);
  if (c) c.textContent = fp(price);
  if (DATA.position.is_open && DATA.position.coin_code === code) updatePosLive();
}

function connectWS() {
  try {
    const ws = new WebSocket("wss://ws.kraken.com");
    ws.onopen = () => ws.send(JSON.stringify({ event: "subscribe", pair: DATA.coins.map(c => c.ws), subscription: { name: "ticker" } }));
    ws.onmessage = ev => {
      const m = JSON.parse(ev.data);
      if (Array.isArray(m) && m.length >= 4 && m[1] && m[1].c) {
        const code = wsMap[m[3]], pr = parseFloat(m[1].c[0]);
        if (code && pr > 0) updateLivePrice(code, pr);
      }
    };
    ws.onclose = () => setTimeout(connectWS, 5000);
  } catch (e) {}
}
async function pollPrices() {
  try {
    const res = await fetch("https://api.kraken.com/0/public/Ticker?pair=" + DATA.coins.map(c => c.kraken).join(","));
    const j = await res.json();
    if (j && j.result) Object.keys(j.result).forEach(k => {
      const p = parseFloat(j.result[k].c[0]);
      DATA.coins.forEach(c => { if (k.includes(c.code) || (c.code === "BTC" && k.includes("XBT")) || (c.code === "DOGE" && k.includes("XDG"))) updateLivePrice(c.code, p); });
    });
  } catch (e) {}
}

function tokenNow() {
  try { const t = localStorage.getItem("gh_token"); if (t) return t; } catch (e) {}
  return window._tok || "";
}
function tokState() { $("tok-state").textContent = tokenNow() ? "impostato ✅" : "non impostato"; }
function setToken() {
  const t = prompt("Incolla il token GitHub (fine-grained, SOLO il repo del bot, permesso Actions: Read and write):");
  if (!t) return;
  window._tok = t.trim();
  try { localStorage.setItem("gh_token", t.trim()); } catch (e) {}
  tokState();
}
async function dispatchBot(command, coin, amount) {
  const log = $("cmd-log");
  if (!tokenNow()) setToken();
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
    if (r.status === 204) { log.textContent = "✅ Comando '" + command + "' inviato. Conferma su Telegram entro 30-90 secondi."; alert("✅ Comando inviato. Attendi la conferma su Telegram."); }
    else { const t = await r.text(); log.textContent = "❌ Errore " + r.status + ": " + t.slice(0, 200); alert("❌ Errore " + r.status); }
  } catch (e) { log.textContent = "❌ Errore di rete: " + e; }
}
function cmdClose() {
  if (!confirm("CHIUDERE a mercato la posizione " + DATA.position.coin_code + " ORA?")) return;
  dispatchBot("close", DATA.position.coin_code, "");
}
function cmdSellAll() {
  const c = $("m-coin").value;
  if (!confirm("VENDERE TUTTO il saldo libero di " + c + " a mercato?")) return;
  dispatchBot("sell_all", c, "");
}
function cmdOpen() {
  if (DATA.position.is_open) { alert("C'e' gia' una posizione aperta su " + DATA.position.coin_code + ": chiudila prima."); return; }
  const c = $("m-coin").value, a = $("m-amt").value;
  if (!confirm("APRIRE LONG a mercato su " + c + (a ? " con " + a + " USDT" : " (importo automatico)") + "?")) return;
  dispatchBot("open", c, a);
}

$("actions-link").href = "https://github.com/" + DATA.repo + "/actions/workflows/" + DATA.workflow;
renderHead(); renderPosition(); renderHistory(); renderMacro(); renderScanner(); tokState();
setInterval(() => { renderHead(); renderMacro(); }, 60000);
connectWS();
setInterval(pollPrices, 10000);
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
# 12. TELEGRAM (solo apertura / chiusura / errori / run manuale)
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
        rank_lines.append(f"{medals[idx]} <b>{ev['code']}</b> ({fmt_price(ev['price'])}): <b>{ev['final_score']}/10</b> ({ev['raw_100']}/100) | FreqAI {ev['ai_prob'] * 100:.0f}% | 15m {t_icon}")

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

    return f"""🤖 <b>PIONEX FREQTRADE + FREQAI v10.0</b>
🕒 <i>{session_str} | BTC Regime: {BTC_REGIME['state']}</i>
━━━━━━━━━━━━━━━━━━━━
<b>{esc(trade_status['headline'])}</b>
{esc(trade_status['detail'])}{alerts_block}

🧭 <b>COSA FARE MANUALMENTE ADESSO:</b>
<b>{esc(advisor['badge'])}</b>
<i>{esc(advisor['instruction'])}</i>

{pos_block}
{closed_block}
• Bilancio Storico: <b>{stats['total_trades']} Trade</b> (Win Rate <b>{wr:.0f}%</b> | PnL Netto: <b>{stats['total_pnl_pct']:+.2f}% / {stats['total_pnl_usdt']:+.2f} USDT</b>)

🏆 <b>TOP 5 CLASSIFICA FREQAI (SU 10 COIN):</b>
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
        log("ERROR", f"Invio messaggio Telegram fallito: {e}")
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
    log("INFO", f"Avvio Pionex Freqtrade + FreqAI v10.0 | Strategia: {STRATEGY.__class__.__name__} | Modalita': {mode}")
    state = load_state()
    pos = state["position"]
    now_utc = datetime.now(timezone.utc)
    in_window = ENTRY_START_UTC <= now_utc.hour < ENTRY_END_UTC
    is_manual_run = GITHUB_EVENT_NAME == "workflow_dispatch"

    if not pos["is_open"] and not in_window and not is_manual_run:
        log("INFO", f"Fuori finestra operativa ({ENTRY_START_UTC:02d}-{ENTRY_END_UTC:02d} UTC) e nessuna posizione: skip.")
        return

    LIVE_PX.update(fetch_pionex_prices())
    eur_usd = fetch_eur_usd_rate()
    fng_val, fng_class = fetch_fear_and_greed()
    macro_events = fetch_macro_calendar()
    futures_map = fetch_all_kraken_futures()
    session_str = skill_nyse_bell_and_session()

    if MANUAL_COMMAND in ("close", "sell_all"):
        tgt = pos["coin_code"] if (MANUAL_COMMAND == "close" or not MANUAL_COIN) else MANUAL_COIN
        scan_list = [COIN_MAP.get(tgt, COINS[0])]
    elif pos["is_open"] and not (in_window or is_manual_run):
        scan_list = [COIN_MAP.get(pos["coin_code"], COINS[0])]
    else:
        scan_list = COINS
    all_evals = scan_all_coins(scan_list, futures_map, fng_val, macro_events)
    update_btc_market_regime(all_evals)

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
        ev["risk_manager"] = skill_budget_and_risk_manager(INVEST_EUR, eur_usd, ev["price"], ev["sl_price"], ev["tp_price"], state, ev["high_soon"], ev["code"], ev=ev)
        ev["exit_plan"] = skill_exit_planner(ev["price"], ev["sl_price"], ev["tp_price"])

    if MANUAL_COMMAND in ("close", "sell_all", "open"):
        log("INFO", f"COMANDO MANUALE: {MANUAL_COMMAND} {MANUAL_COIN} {MANUAL_USDT or ''}")
        trade_status = run_manual_command(state, all_evals, eur_usd, MANUAL_COMMAND, MANUAL_COIN, MANUAL_USDT)
    else:
        trade_status = manage_autonomous_trading(state, ranked_evals, all_evals, eur_usd)
    generate_webapp_html(ranked_evals, state, trade_status, fng_val, fng_class, session_str, eur_usd, macro_events)

    action_state = trade_status["action_state"]
    notify_states = ("OPENED_NOW", "CLOSED_NOW", "CLOSE_FAILED", "ENTRY_FAILED", "MANUAL_DONE", "MANUAL_NOOP")
    if action_state in notify_states or is_manual_run:
        log("INFO", f"Invio notifica Telegram (action_state={action_state}, manual_run={is_manual_run})")
        send_telegram(build_telegram_report(ranked_evals, state, trade_status, session_str))
    else:
        best = ranked_evals[0]
        log("INFO", f"Giro completato ({action_state} | BTC: {BTC_REGIME['state']} | Migliore: {best['code']} {best['raw_100']}/100). Nessun messaggio Telegram.")
    log("INFO", "Esecuzione completata.")

    if action_state in ("CLOSE_FAILED", "ENTRY_FAILED"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
