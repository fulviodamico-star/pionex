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
GITHUB_TOKEN = (os.getenv("GH_PAT") or os.getenv("GITHUB_TOKEN") or "").strip()
GITHUB_REPO = _env("GITHUB_REPOSITORY", "")
GITHUB_EVENT_NAME = _env("GITHUB_EVENT_NAME", "manual")

MANUAL_COMMAND = _env("MANUAL_COMMAND", "").lower()
MANUAL_COIN = _env("MANUAL_COIN", "").upper()
try:
    MANUAL_USDT = float(_env("MANUAL_USDT", 0))
except ValueError:
    MANUAL_USDT = 0.0

INVEST_EUR = float(_env("INVEST_EUR", 50.0))
AUTO_TRADE_ENABLED = _env("AUTO_TRADE_ENABLED", "false").lower() == "true"

# Modalita' Continua / Tick Guardian
DAEMON_MODE = _env("DAEMON_MODE", "false").lower() == "true"  # True per server/PC 24/7 infinito
GUARDIAN_MINUTES_OPEN = float(_env("GUARDIAN_MINUTES_OPEN", 18.0))  # Minuti di guardia continua con trade aperto
GUARDIAN_MINUTES_HOT = float(_env("GUARDIAN_MINUTES_HOT", 6.0))     # Minuti di guardia se una coin e' quasi pronta (>=54/100)
GUARDIAN_TICK_SEC = int(_env("GUARDIAN_TICK_SEC", 15))              # Controllo prezzi Pionex + Telegram ogni 15s
GUARDIAN_RESCAN_SEC = int(_env("GUARDIAN_RESCAN_SEC", 300))         # Rianalisi candele 15m/AI ogni 5 min
SELF_CHAIN_WHEN_OPEN = _env("SELF_CHAIN_WHEN_OPEN", "true").lower() == "true"
PARTIAL_TP_ENABLED = _env("PARTIAL_TP_ENABLED", "true").lower() == "true"

# Ingressi / rischio
Entry_Score_Min = int(_env("ENTRY_SCORE_MIN", 6))
Entry_Raw_100_Min = int(_env("ENTRY_RAW_100_MIN", 58))
RISK_PER_TRADE_PCT = float(_env("RISK_PER_TRADE_PCT", 1.5))
DAILY_MAX_LOSS_PCT = float(_env("DAILY_MAX_LOSS_PCT", 4.0))
MAX_TRADES_PER_DAY = int(_env("MAX_TRADES_PER_DAY", 5))
MAX_CONSEC_LOSSES = int(_env("MAX_CONSEC_LOSSES", 3))
MIN_REWARD_RISK_R = 1.15
CASH_BUFFER_PCT = 5.0
MIN_ORDER_USDT = float(_env("MIN_ORDER_USDT", 10.5))
FEE_PCT_PER_SIDE = float(_env("FEE_PCT_PER_SIDE", 0.05))
PIONEX_FEE_ROUNDTRIP_PCT = 2.0 * FEE_PCT_PER_SIDE
SCALP_MIN_NET_PROFIT_PCT = 0.65
TIME_STOP_PROFIT_HOURS = 4.5
TIME_STOP_STALE_HOURS = 6.0

# Finestra operativa (UTC)
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
LIVE_PX = {}
BTC_REGIME = {"state": "NEUTRAL", "rsi_15m": 50.0, "ret_1h_pct": 0.0, "allow_alts": True, "label": "⚪ BTC Neutrale"}

FREQTRADE_CONFIG = {
    "stake_currency": "USDT",
    "dry_run": not bool(PIONEX_API_KEY and PIONEX_API_SECRET and AUTO_TRADE_ENABLED),
    "timeframe": "1h",
    "freqai": {
        "enabled": True,
        "conv_width": 1,
        "train_period_candles": 320,
        "backtest_period_candles": 80,
        "identifier": "pionex_triple_barrier_v10",
        "feature_parameters": {
            "include_timeframes": ["15m", "1h", "4h"],
            "indicator_periods_candles": [14, 20, 50],
            "label_period_candles": 5,
            "DI_threshold": 2.2,
            "weight_factor": 0.9,
        },
    },
}


# ==============================================================================
# 1. UTILITA'
# ==============================================================================
_SECRETS = [s for s in (TELEGRAM_TOKEN, PIONEX_API_KEY, PIONEX_API_SECRET, GITHUB_TOKEN) if s]


def redact(text):
    t = str(text)
    for s in _SECRETS:
        t = t.replace(s, "***")
    return t


def log(level, msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}][{level}] {redact(msg)}", flush=True)


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
            "tp2_locked": False, "partial_tp_done": False, "realized_partial_usdt": 0.0,
            "alerts_triggered": [], "opened_at": "", "opened_ts": 0,
            "last_check_ts": 0, "close_fail_count": 0, "mode": "SIMULATION", "order_id": "",
            "enter_tag": "freqai_fastscalp"
        },
        "stats": {
            "total_trades": 0, "wins": 0, "losses": 0, "total_pnl_pct": 0.0, "total_pnl_usdt": 0.0,
            "daily_date": "", "daily_pnl_pct": 0.0, "daily_pnl_usdt": 0.0, "daily_trades": 0,
            "consec_losses": 0, "last_close_ts": 0, "last_loss_ts": 0, "coin_last_close": {},
            "last_tg_update_id": 0, "last_action": "Nessuna operazione recente"
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
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log("ERROR", f"Salvataggio {STATE_FILE} fallito: {e}")


# ==============================================================================
# 3. CLIENT PIONEX (con supporto vendita parziale Scale-Out)
# ==============================================================================
def pionex_live_enabled():
    return bool(PIONEX_API_KEY and PIONEX_API_SECRET and AUTO_TRADE_ENABLED)


def pionex_request(method, path, params=None, body=None, timeout=HTTP_TIMEOUT):
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


def live_sell(pos, coin_cfg, price, fraction=1.0):
    """Vendita totale (fraction=1.0) o parziale Scale-Out (es. fraction=0.5) con riconciliazione."""
    base, sym = coin_cfg["code"], coin_cfg["pionex"]
    fraction = clamp(fraction, 0.1, 1.0)
    target_coin = pos["amount_coin"] * fraction
    invested_slice = pos["invested_usdt"] * fraction

    if pos.get("mode") != "LIVE_PIONEX" or not pionex_live_enabled():
        scale = max(coin_cfg["prec"], 2)
        sold_qty = floor_to_scale(target_coin, scale) if fraction < 1.0 else pos["amount_coin"]
        proceeds = sold_qty * price * (1 - FEE_PCT_PER_SIDE / 100.0)
        return {"ok": True, "exit_price": price, "sold_qty": sold_qty, "proceeds_usdt": proceeds,
                "pnl_usdt": proceeds - invested_slice, "msg": "Vendita simulata"}

    rules = pionex_symbol_rules(sym)
    bal0 = pionex_get_balances()
    if bal0 is None:
        return {"ok": False, "msg": "Impossibile leggere i saldi: vendita non inviata (riprovo al prossimo giro)"}
    free = bal0.get(base, {}).get("free", 0.0)
    size = floor_to_scale(min(target_coin, free), rules["base_scale"])
    if size <= 0 or size * price < rules["min_amount"] * 0.98:
        return {"ok": False, "msg": f"Quantita' vendibile {size} {base} sotto il minimo ordine Pionex"}

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
    return {"ok": True, "exit_price": exit_price, "sold_qty": size, "proceeds_usdt": proceeds,
            "pnl_usdt": proceeds - invested_slice, "msg": res["msg"]}


# ==============================================================================
# 4. ACQUISIZIONE DATI E COMANDI TELEGRAM DIRETTI
# ==============================================================================
def poll_telegram_commands(state):
    """Legge comandi inviati direttamente nella chat Telegram (/status, /close, /open SOL, /sellall SOL)."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return None
    offset = int(state["stats"].get("last_tg_update_id", 0)) + 1
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        r = SESSION.get(url, params={"offset": offset, "limit": 10, "timeout": 1}, timeout=5)
        if r.status_code != 200:
            return None
        j = r.json()
        if not j.get("ok"):
            return None
    except Exception:
        return None

    cmd_found = None
    for upd in j.get("result", []):
        uid = int(upd.get("update_id", 0))
        if uid > state["stats"].get("last_tg_update_id", 0):
            state["stats"]["last_tg_update_id"] = uid
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if chat_id != str(TELEGRAM_CHAT_ID):
            continue
        # Ignora comandi piu' vecchi di 15 minuti
        if time.time() - int(msg.get("date", 0)) > 900:
            continue
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        parts = text.split()
        cmd_raw = parts[0].lstrip("/").split("@")[0].lower()
        coin_arg = parts[1].upper() if len(parts) >= 2 else ""
        amt_arg = 0.0
        if len(parts) >= 3:
            try:
                amt_arg = float(parts[2])
            except ValueError:
                amt_arg = 0.0

        if cmd_raw in ("close", "chiudi", "panic", "stop"):
            cmd_found = {"command": "close", "coin": coin_arg, "usdt": 0.0}
        elif cmd_raw in ("sellall", "sell_all", "venditutto"):
            cmd_found = {"command": "sell_all", "coin": coin_arg, "usdt": 0.0}
        elif cmd_raw in ("open", "buy", "apri", "long"):
            cmd_found = {"command": "open", "coin": coin_arg, "usdt": amt_arg}
        elif cmd_raw in ("status", "stato", "report"):
            cmd_found = {"command": "status", "coin": "", "usdt": 0.0}

    if cmd_found:
        save_state(state)
    return cmd_found


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
# 5. INDICATORI TECNICI + EMULATORE `ta` & `qtpylib`
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
# 6. FREQAI v10 (Triple-Barrier Labeling + Dissimilarity Index + 4-Model Ensemble)
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
        f6 = ((c - candles[i - 1]) / atr) if i > 0 else (((c - candles_1h[i]["low"]) / rng) * 2.0 - 1.0)
        features.append([f1, f2, f3, f4, f5, clamp(f6, -2.5, 2.5)])
    return features


def build_triple_barrier_labels(candles_1h, horizon=5, tp_atr_mult=1.35, sl_atr_mult=1.0):
    """
    Triple-Barrier Labeling (Marcos López de Prado):
    Etichetta 1.0 se nei successivi `horizon` periodi il prezzo tocca il Take-Profit
    prima dello Stop-Loss; 0.0 se tocca prima lo Stop-Loss.
    """
    n = len(candles_1h)
    closes = [c["close"] for c in candles_1h]
    highs = [c["high"] for c in candles_1h]
    lows = [c["low"] for c in candles_1h]
    atrs = calc_atr_series(candles_1h, 14)
    labels = [0.5] * n
    for i in range(30, n - horizon - 1):
        entry = closes[i]
        risk = max(atrs[i] * 1.10, entry * 0.009)
        tp = entry + tp_atr_mult * risk
        sl = entry - sl_atr_mult * risk
        outcome = None
        for h in range(1, horizon + 1):
            if lows[i + h] <= sl:
                outcome = 0.0
                break
            if highs[i + h] >= tp:
                outcome = 1.0
                break
        if outcome is None:
            outcome = 1.0 if closes[i + horizon] > entry + 0.15 * risk else 0.0
        labels[i] = outcome
    return labels


def lorentzian_knn_predict(candles_1h, features, tb_labels, horizon=5, k=18):
    n = len(candles_1h)
    if n < 120:
        return 0.50
    curr_x = features[-1]
    candidates = []
    for i in range(35, n - horizon - 1):
        dist = sum(math.log(1.0 + abs(a - b)) for a, b in zip(curr_x, features[i]))
        candidates.append((dist, tb_labels[i]))
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


def online_mlp_neural_net(candles_1h, features, tb_labels, horizon=5, epochs=35, lr=0.045):
    n = len(candles_1h)
    if n < 120:
        return 0.50, 0.50
    dataset = [(features[i], tb_labels[i]) for i in range(max(35, n - 320), n - horizon - 1)]
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

    hits = sum(1 for x, y in hold if (forward(x)[0] > 0.5) == (y >= 0.5))
    acc = hits / len(hold)
    up_rate = sum(1.0 for _, y in hold if y >= 0.5) / len(hold)
    edge = acc - max(up_rate, 1.0 - up_rate)
    shrink = clamp(edge / 0.06, 0.15, 1.0)
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


class FreqAIEngine:
    def __init__(self, config):
        self.config = config
        self.freqai_cfg = config.get("freqai", {})
        self.fp = self.freqai_cfg.get("feature_parameters", {})
        self.di_threshold = float(self.fp.get("DI_threshold", 2.2))
        self.horizon = int(self.fp.get("label_period_candles", 5))

    def start(self, dataframe, metadata, strategy):
        candles_1h = metadata["candles_1h"]
        for period in self.fp.get("indicator_periods_candles", [14, 20, 50]):
            dataframe = strategy.feature_engineering_expand_all(dataframe, period, metadata=metadata)
        dataframe = strategy.feature_engineering_expand_basic(dataframe, metadata=metadata)
        dataframe = strategy.feature_engineering_standard(dataframe, metadata=metadata)
        dataframe = strategy.set_freqai_targets(dataframe, metadata=metadata)

        features = build_feature_matrix(candles_1h)
        tb_labels = build_triple_barrier_labels(candles_1h, horizon=self.horizon)

        curr_x = features[-1]
        hist_x = features[max(35, len(features) - 160):-1]
        if hist_x:
            dists = [math.sqrt(sum((a - b) ** 2 for a, b in zip(curr_x, hx)) / len(curr_x)) for hx in hist_x]
            di_val = min(dists) if dists else 0.5
        else:
            di_val = 0.5
        do_predict = 1 if di_val <= self.di_threshold else 0

        prob_knn = lorentzian_knn_predict(candles_1h, features, tb_labels, horizon=self.horizon, k=18)
        prob_mlp, mlp_acc = online_mlp_neural_net(candles_1h, features, tb_labels, horizon=self.horizon, epochs=35, lr=0.045)

        if HAS_SKLEARN and len(candles_1h) >= 140:
            try:
                X_all = np.array(features[35:-self.horizon - 1])
                y_all = np.array([1 if tb_labels[i] >= 0.5 else 0 for i in range(35, len(candles_1h) - self.horizon - 1)])
                if len(set(y_all)) > 1:
                    clf = HistGradientBoostingClassifier(max_depth=3, max_iter=55, random_state=42)
                    clf.fit(X_all, y_all)
                    prob_sk = float(clf.predict_proba(np.array([curr_x]))[0][1])
                    prob_mlp = 0.60 * prob_mlp + 0.40 * prob_sk
            except Exception:
                pass

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
            "prob_mc": prob_mc, "mlp_holdout_acc": mlp_acc, "di_values": di_val,
            "do_predict": do_predict, "tm_stats": tm_stats,
        }
        return dataframe


# ==============================================================================
# 7. PARAMETRI HYPEROPT E INTERFACCIA FREQTRADE `IStrategy` v3
# ==============================================================================
class IntParameter:
    """Compatibile con `freqtrade.strategy.IntParameter` per Hyperopt."""
    def __init__(self, low: int, high: int, default: int, space: str = "buy", optimize: bool = True):
        self.low = low
        self.high = high
        self.value = default
        self.space = space
        self.optimize = optimize


class DecimalParameter:
    """Compatibile con `freqtrade.strategy.DecimalParameter` per Hyperopt."""
    def __init__(self, low: float, high: float, default: float, decimals: int = 2, space: str = "buy", optimize: bool = True):
        self.low = low
        self.high = high
        self.value = round(default, decimals)
        self.space = space
        self.optimize = optimize


@dataclass
class Trade:
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
    partial_tp_done: bool = False
    alerts_triggered: list = field(default_factory=list)

    def calc_profit_ratio(self, rate: float) -> float:
        if self.open_rate <= 0:
            return 0.0
        gross = (rate - self.open_rate) / self.open_rate
        return gross - (PIONEX_FEE_ROUNDTRIP_PCT / 100.0)


class IStrategy:
    INTERFACE_VERSION: int = 3
    timeframe: str = "1h"
    informative_timeframe: str = "4h"
    detail_timeframe: str = "15m"
    minimal_roi: dict = {"0": 0.025, "270": 0.0035}
    stoploss: float = -0.020
    trailing_stop: bool = True
    use_custom_stoploss: bool = True
    use_exit_signal: bool = True
    position_adjustment_enable: bool = True
    process_only_new_candles: bool = True
    startup_candle_count: int = 60

    def __init__(self, config: dict):
        self.config = config
        self.freqai = FreqAIEngine(config)

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

    def adjust_trade_position(self, trade: Trade, current_time: datetime, current_rate: float,
                              current_profit: float, min_stake: float, max_stake: float, **kwargs):
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
    INTERFACE_VERSION = 3
    timeframe = "1h"
    minimal_roi = {"0": 0.025, "270": 0.0035}
    stoploss = -0.020
    trailing_stop = True
    use_custom_stoploss = True
    position_adjustment_enable = True

    # Hyperopt Parameters (Freqtrade Standard)
    buy_score_min = IntParameter(50, 72, default=Entry_Raw_100_Min, space="buy")
    buy_ai_prob_min = DecimalParameter(0.48, 0.65, default=0.50, decimals=2, space="buy")
    sell_score_defensive = IntParameter(2, 4, default=3, space="sell")

    @property
    def protections(self):
        return [
            {"method": "CooldownPeriod", "stop_duration": MIN_MINUTES_BETWEEN_TRADES},
            {"method": "StoplossGuard", "lookback_period": 1440, "trade_limit": MAX_CONSEC_LOSSES,
             "stop_duration": COOLDOWN_AFTER_LOSS_MIN, "only_per_pair": False},
            {"method": "MaxDrawdown", "lookback_period": 1440, "max_allowed_drawdown": DAILY_MAX_LOSS_PCT / 100.0},
        ]

    def feature_engineering_expand_all(self, dataframe, period: int, metadata: dict, **kwargs):
        if HAS_PANDAS and isinstance(dataframe, pd.DataFrame):
            dataframe[f"%-rsi-period_{period}"] = ta.RSI(dataframe, timeperiod=period)
            dataframe[f"%-ema-period_{period}"] = ta.EMA(dataframe, timeperiod=period)
            dataframe[f"%-atr-period_{period}"] = ta.ATR(dataframe, timeperiod=period)
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

        dataframe = self.freqai.start(dataframe, metadata, self)
        fai = metadata["freqai_result"]
        ai_ensemble_prob = fai["ai_prob"]
        tm_stats = fai["tm_stats"]

        scanner_timer = skill_chart_scanner_entry_timer(candles_15m, vol_z, ob["imbalance"])

        # Aggiorna il Regime Globale di BTC quando viene analizzato BTC
        if metadata["code"] == "BTC":
            ret_1h = ((closes_15m[-1] - closes_15m[-4]) / closes_15m[-4] * 100.0) if len(closes_15m) >= 5 else 0.0
            vwap_btc = scanner_timer["vwap_15m"]
            is_dump = (rsi_15m < 32.0 and ret_1h < -1.1) or (vwap_btc > 0 and price < vwap_btc * 0.987 and ob["imbalance"] < -0.10)
            is_bull = price >= ema20_1h and rsi_15m >= 52.0 and ret_1h >= 0.0
            BTC_REGIME.update({
                "state": "BTC_DUMP_GUARD" if is_dump else ("BULL_IMPULSE" if is_bull else "NEUTRAL"),
                "rsi_15m": round(rsi_15m, 1),
                "ret_1h_pct": round(ret_1h, 2),
                "allow_alts": not is_dump,
                "label": "🚨 BTC Dump Guard (Altcoin Bloccate)" if is_dump else ("🟢 BTC Bull Impulse" if is_bull else "⚪ BTC Neutrale / Range"),
            })

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

        # B. FreqAI Triple-Barrier + Time Machine (max 25)
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
            "ref_close_15m": closes_15m[-1], "sl_price": sl_price, "tp_price": tp_price,
            "atr_1h": atr_1h, "atr_pct": atr_pct, "raw_100": raw_100, "final_score": final_score,
            "score_trend": score_trend, "score_ai": score_ai, "score_mom": score_mom,
            "score_micro": score_micro, "score_macro": score_macro, "ai_prob": ai_ensemble_prob,
            "prob_mlp": fai["prob_mlp"], "prob_knn": fai["prob_knn"], "prob_mc": fai["prob_mc"],
            "mlp_holdout_acc": fai["mlp_holdout_acc"], "di_values": fai["di_values"],
            "do_predict": fai["do_predict"], "rsi_1h": rsi_1h, "rsi_4h": rsi_4h,
            "rsi_15m": rsi_15m, "rsi_15m_prev": rsi_15m_prev, "adx_1h": adx_1h,
            "hurst": hurst, "z_linreg": z_linreg, "poc_price": poc_price, "vol_z": vol_z,
            "ob": ob, "fut": fut, "st_dir_1h": st_dir_1h, "tm_stats": tm_stats,
            "scanner_timer": scanner_timer, "high_soon": bool(high_soon),
            "c15_recent": [{"time": c["time"], "high": c["high"], "low": c["low"]} for c in candles_15m[-24:]],
        }
        return dataframe

    def populate_entry_trend(self, dataframe, metadata: dict):
        ev = metadata["eval"]
        btc_ok = True if ev["code"] == "BTC" else BTC_REGIME.get("allow_alts", True)
        cond = (
            btc_ok
            and ev["final_score"] >= Entry_Score_Min
            and ev["raw_100"] >= self.buy_score_min.value
            and ev["ai_prob"] >= self.buy_ai_prob_min.value
            and ev["scanner_timer"]["triggered"]
        )
        tag = f"freqai_tb_{ev['raw_100']}_ai_{int(ev['ai_prob'] * 100)}"
        if HAS_PANDAS and isinstance(dataframe, pd.DataFrame):
            dataframe["enter_long"] = 0
            dataframe["enter_tag"] = ""
            if cond and len(dataframe) > 0:
                dataframe.iloc[-1, dataframe.columns.get_loc("enter_long")] = 1
                dataframe.iloc[-1, dataframe.columns.get_loc("enter_tag")] = tag
        ev["enter_long"] = 1 if cond else 0
        ev["enter_tag"] = tag if cond else ""
        return dataframe

    def populate_exit_trend(self, dataframe, metadata: dict):
        ev = metadata["eval"]
        cond = ev["final_score"] <= self.sell_score_defensive.value and ev["ai_prob"] < 0.40
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

    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float,
                            proposed_stake: float, min_stake: float, max_stake: float,
                            leverage: float, entry_tag: str, side: str, **kwargs) -> float:
        """Dynamic Kelly / Confidence Scaling entro i limiti di sicurezza."""
        ev = kwargs.get("ev", {})
        ai_prob = ev.get("ai_prob", 0.55)
        do_predict = ev.get("do_predict", 1)
        mult = clamp(0.85 + (ai_prob - 0.50) * 1.0, 0.80, 1.05)
        if not do_predict:
            mult *= 0.85
        stake = min(proposed_stake * mult, max_stake)
        return max(stake, min_stake) if max_stake >= min_stake else max_stake

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float,
                            time_in_force: str, current_time: datetime, entry_tag: str,
                            side: str, **kwargs) -> bool:
        """Filtro Anti-Slippage e Spread prima dell'invio dell'ordine Market."""
        ev = kwargs.get("ev", {})
        spread_bps = ev.get("ob", {}).get("spread_bps", 5.0)
        ref_close = ev.get("ref_close_15m", rate)
        if spread_bps > 25.0:
            log("WARN", f"confirm_trade_entry rifiutato su {pair}: spread troppo alto ({spread_bps:.1f} bps)")
            return False
        if ref_close > 0 and (rate - ref_close) / ref_close > 0.015:
            log("WARN", f"confirm_trade_entry rifiutato su {pair}: spike improvviso +{(rate/ref_close - 1)*100:.2f}% sopra close 15m")
            return False
        return True

    def adjust_trade_position(self, trade: Trade, current_time: datetime, current_rate: float,
                              current_profit: float, min_stake: float, max_stake: float, **kwargs):
        """
        Scale-Out Parziale Freqtrade: a +1.15R incassa il 50% della posizione se ambedue le meta'
        superano il minimo ordine di Pionex (~10.5 USDT).
        """
        if not PARTIAL_TP_ENABLED or trade.partial_tp_done:
            return None
        current_r = (current_rate - trade.open_rate) / trade.one_r if trade.one_r > 0 else 0.0
        current_val_usdt = trade.amount * current_rate
        if current_r >= 1.15 and current_val_usdt >= 2.25 * min_stake:
            return -(trade.stake_amount * 0.50)  # Negativo in Freqtrade = riduzione parziale (Scale-Out 50%)
        return None

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime,
                        current_rate: float, current_profit: float, after_fill: bool = False, **kwargs) -> float:
        open_eval = kwargs.get("open_eval", {})
        entry = trade.open_rate
        sl = trade.stop_loss
        one_r = trade.one_r
        current_r = (current_rate - entry) / one_r if one_r > 0 else 0.0
        be_fee_price = round(entry * (1.0 + (PIONEX_FEE_ROUNDTRIP_PCT + 0.05) / 100.0), 6)
        trail_msg = ""

        # Se BTC entra in Dump Guard e siamo in leggero profitto su un'altcoin, blinda subito il Break-Even
        btc_dump_lock = (trade.coin_code != "BTC" and not BTC_REGIME.get("allow_alts", True) and current_r >= 0.35)

        if not trade.breakeven_locked and (current_r >= 0.65 or btc_dump_lock or (current_rate >= entry * 1.0055 and open_eval.get("rsi_15m", 50.0) >= 66.0)):
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
# 8. SKILL OPERATIVE E PROTECTIONS
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


def skill_budget_and_risk_manager(invest_eur, eur_usd, entry_price, sl_price, tp_price, state, macro_high_soon, code=None, ev=None):
    total_usdt = invest_eur * eur_usd
    cash_buffer_usdt = total_usdt * (CASH_BUFFER_PCT / 100.0)
    usable_usdt = total_usdt - cash_buffer_usdt

    sl_dist_pct = ((entry_price - sl_price) / entry_price) * 100.0 if entry_price > 0 else 2.0
    tp_dist_pct = ((tp_price - entry_price) / entry_price) * 100.0 if entry_price > 0 else 2.5
    fee_rt = PIONEX_FEE_ROUNDTRIP_PCT
    rr_ratio = (tp_dist_pct - fee_rt) / (sl_dist_pct + fee_rt) if sl_dist_pct > 0 else 0.0

    risk_usdt = total_usdt * (RISK_PER_TRADE_PCT / 100.0)
    proposed_usdt = risk_usdt / ((sl_dist_pct + fee_rt) / 100.0) if sl_dist_pct > 0 else usable_usdt
    now = datetime.now(timezone.utc)
    position_usdt = STRATEGY.custom_stake_amount(
        pair=f"{code}_USDT" if code else "SOL_USDT", current_time=now, current_rate=entry_price,
        proposed_stake=proposed_usdt, min_stake=MIN_ORDER_USDT, max_stake=usable_usdt,
        leverage=1.0, entry_tag="", side="long", ev=ev or {}
    )
    max_loss_usdt = position_usdt * ((sl_dist_pct + fee_rt) / 100.0)
    expected_gain_usdt = position_usdt * ((tp_dist_pct - fee_rt) / 100.0)

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
    if code and code != "BTC" and not BTC_REGIME.get("allow_alts", True):
        reasons.append("BTC Dump Guard attivo (protezione crollo BTC 15m)")
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
# 9. VALUTAZIONE COIN (BTC Analizzato per Primo per il Regime Filter)
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
    ref_price = candles_15m[-1]["close"]
    px = LIVE_PX.get(coin_cfg["pionex"])
    price = px if (px and abs(px / ref_price - 1.0) < 0.03) else ref_price

    dataframe = candles_to_dataframe(candles_1h)
    metadata = {
        "pair": coin_cfg["pionex"], "code": code, "coin_cfg": coin_cfg, "price": price,
        "candles_1h": candles_1h, "candles_4h": candles_4h, "candles_15m": candles_15m,
        "ob": ob, "fut": fut, "fng_val": fng_val, "macro_events": macro_events,
    }

    dataframe = STRATEGY.populate_indicators(dataframe, metadata)
    dataframe = STRATEGY.populate_entry_trend(dataframe, metadata)
    dataframe = STRATEGY.populate_exit_trend(dataframe, metadata)
    return metadata["eval"]


def make_fallback_eval(coin_cfg, pos):
    px = LIVE_PX.get(coin_cfg["pionex"])
    price = px or pos.get("entry_price", 0.0)
    return {
        "coin": coin_cfg, "code": coin_cfg["code"], "price": price, "ref_close_15m": price,
        "sl_price": pos.get("sl_price", 0.0), "tp_price": pos.get("tp_price", 0.0),
        "atr_1h": 0.0, "atr_pct": 0.0, "raw_100": 0, "final_score": 5, "ai_prob": 0.5,
        "prob_mlp": 0.5, "prob_knn": 0.5, "prob_mc": 0.5, "mlp_holdout_acc": 0.5,
        "rsi_1h": 50.0, "rsi_4h": 50.0, "rsi_15m": 50.0, "rsi_15m_prev": 50.0,
        "ob": {"imbalance": 0.0, "spread_bps": 0.0, "bid_ask_ratio": 1.0},
        "tm_stats": {"win_rate": 50.0, "expectancy_r": 0.0, "matches": 0},
        "scanner_timer": {"triggered": False, "triggers": 0, "vwap_15m": 0.0, "summary": "Dati non disponibili"},
        "high_soon": False, "c15_recent": [], "stale": not bool(px), "fallback": True,
        "enter_long": 0, "enter_tag": "", "exit_long": 0, "exit_tag": "",
    }


def scan_all_coins(coins, futures_map, fng_val, macro_events):
    results = {}
    # Analizza sempre BTC per primo per popolare BTC_REGIME prima di valutare le altcoin
    btc_cfg = COIN_MAP.get("BTC")
    if btc_cfg and any(c["code"] == "BTC" for c in coins):
        try:
            btc_res = evaluate_coin(btc_cfg, futures_map, fng_val, macro_events)
            if btc_res:
                results["BTC"] = btc_res
        except Exception as e:
            log("WARN", f"Errore analisi BTC: {e}")

    rem_coins = [c for c in coins if c["code"] not in results]
    if rem_coins:
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_to_code = {executor.submit(evaluate_coin, cfg, futures_map, fng_val, macro_events): cfg["code"] for cfg in rem_coins}
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
# 10. MOTORE AUTONOMO + SCALE-OUT + TICK GUARDIAN REAL-TIME
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
    partial_pnl = float(pos.get("realized_partial_usdt", 0.0))
    total_pnl_usdt = close["pnl_usdt"] + partial_pnl
    orig_invested = pos["invested_usdt"] * (2.0 if pos.get("partial_tp_done") else 1.0)
    net_pnl_pct = (total_pnl_usdt / orig_invested) * 100.0 if orig_invested > 0 else 0.0
    exit_price = close.get("exit_price", curr_price)

    stats["total_trades"] += 1
    if total_pnl_usdt >= 0:
        stats["wins"] += 1
        stats["consec_losses"] = 0
    else:
        stats["losses"] += 1
        stats["consec_losses"] = stats.get("consec_losses", 0) + 1
        stats["last_loss_ts"] = now_ts
    stats["total_pnl_pct"] = round(stats["total_pnl_pct"] + net_pnl_pct, 2)
    stats["total_pnl_usdt"] = round(stats["total_pnl_usdt"] + total_pnl_usdt, 2)
    stats["daily_pnl_usdt"] = round(stats.get("daily_pnl_usdt", 0.0) + total_pnl_usdt, 2)
    stats["daily_pnl_pct"] = round(stats.get("daily_pnl_pct", 0.0) + (total_pnl_usdt / capital_usdt) * 100.0, 2)
    stats["last_close_ts"] = now_ts
    stats.setdefault("coin_last_close", {})[open_code] = now_ts
    stats["last_action"] = f"CHIUSO {open_code} ({net_pnl_pct:+.2f}%) il {now_str}"

    state.setdefault("history", []).insert(0, {
        "coin": open_code, "entry": entry, "exit": exit_price, "pnl_pct": round(net_pnl_pct, 2),
        "pnl_usdt": round(total_pnl_usdt, 2), "reason": close_reason, "closed_at": now_str, "mode": pos.get("mode", "SIMULATION")})
    state["history"] = state["history"][:25]
    pos["is_open"] = False
    pos["close_fail_count"] = 0
    pos["partial_tp_done"] = False
    pos["realized_partial_usdt"] = 0.0
    save_state(state)
    return {"action_state": "CLOSED_NOW", "headline": f"🔔 POSIZIONE {open_code} CHIUSA AUTOMATICAMENTE",
            "detail": f"{close_reason} | PnL Netto Totale: {net_pnl_pct:+.2f}% ({total_pnl_usdt:+.2f} USDT) | {close['msg']}",
            "pnl_pct": net_pnl_pct, "pnl_usdt": total_pnl_usdt, "current_r": 0.0, "alerts_fired": alerts_fired}


def _manage_open_position(state, all_evals, capital_usdt, now_str, now_ts):
    pos = state["position"]
    open_code = pos.get("coin_code", "SOL")
    coin_cfg = COIN_MAP.get(open_code, COINS[0])
    open_eval = all_evals[open_code]
    curr_price = LIVE_PX.get(coin_cfg["pionex"]) or open_eval["price"]
    open_eval["price"] = curr_price
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

    if open_eval.get("stale") and not LIVE_PX.get(coin_cfg["pionex"]):
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
    pnl_usdt_live = est_proceeds - pos["invested_usdt"] + float(pos.get("realized_partial_usdt", 0.0))
    orig_inv = pos["invested_usdt"] * (2.0 if pos.get("partial_tp_done") else 1.0)
    net_pnl_pct = (pnl_usdt_live / orig_inv) * 100.0 if orig_inv > 0 else 0.0
    current_r = (curr_price - entry) / one_r if one_r > 0 else 0.0

    now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    trade = Trade(
        pair=coin_cfg["pionex"], coin_code=open_code, open_rate=entry,
        amount=pos["amount_coin"], stake_amount=pos["invested_usdt"],
        open_date_utc=datetime.fromtimestamp(opened_ts, tz=timezone.utc),
        stop_loss=sl, initial_stop_loss=pos.get("initial_sl", sl),
        take_profit=tp, one_r=one_r, max_rate=highest,
        enter_tag=pos.get("enter_tag", "freqai_fastscalp"),
        breakeven_locked=pos.get("breakeven_locked", False),
        tp1_locked=pos.get("tp1_locked", False),
        tp2_locked=pos.get("tp2_locked", False),
        partial_tp_done=pos.get("partial_tp_done", False),
        alerts_triggered=list(pos.get("alerts_triggered", [])),
    )

    # Check Scale-Out Parziale (`adjust_trade_position`)
    adj = STRATEGY.adjust_trade_position(
        trade=trade, current_time=now_dt, current_rate=curr_price,
        current_profit=net_pnl_pct / 100.0, min_stake=MIN_ORDER_USDT, max_stake=capital_usdt
    )
    if adj is not None and adj < 0 and not pos.get("partial_tp_done"):
        part = live_sell(pos, coin_cfg, curr_price, fraction=0.5)
        if part["ok"]:
            pos["amount_coin"] = max(0.0, pos["amount_coin"] - part["sold_qty"])
            pos["invested_usdt"] = pos["invested_usdt"] * 0.5
            pos["partial_tp_done"] = True
            pos["realized_partial_usdt"] = round(float(pos.get("realized_partial_usdt", 0.0)) + part["pnl_usdt"], 4)
            alerts_fired.append(f"💰 Scale-Out 50% eseguito a {fmt_price(curr_price)} ({part['pnl_usdt']:+.2f} USDT incassati)")

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
    alerts_fired.extend([a for a in trade.alerts_triggered[prev_alerts_len:] if a != "R190"])
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
        close = live_sell(pos, coin_cfg, curr_price, fraction=1.0)
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
    now_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    usdt = round(best["risk_manager"]["position_usdt"], 2)
    if not STRATEGY.confirm_trade_entry(
        pair=best["coin"]["pionex"], order_type="market", amount=usdt / max(best["price"], 1e-8),
        rate=best["price"], time_in_force="GTC", current_time=now_dt,
        entry_tag=best.get("enter_tag", ""), side="long", ev=best
    ):
        return {"action_state": "WAITING", "headline": f"🛡️ INGRESSO {best['code']} SOSPESO (Filtro Anti-Slippage)",
                "detail": f"Segnale su {best['code']} presente ma spread/spike eccessivo negli ultimi secondi. Riprovo al prossimo tick.",
                "pnl_pct": 0.0, "pnl_usdt": 0.0, "current_r": 0.0, "alerts_fired": []}

    return _execute_open(state, best, usdt, now_str, now_ts)


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
        "breakeven_locked": False, "tp1_locked": False, "tp2_locked": False,
        "partial_tp_done": False, "realized_partial_usdt": 0.0, "alerts_triggered": [],
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
        price = LIVE_PX.get(cfg["pionex"]) or all_evals[code]["price"]
        close = live_sell(pos, cfg, price, fraction=1.0)
        if not close["ok"]:
            pos["close_fail_count"] = pos.get("close_fail_count", 0) + 1
            save_state(state)
            return {"action_state": "CLOSE_FAILED", "headline": f"🚨 CHIUSURA MANUALE {code} FALLITA",
                    "detail": f"{close['msg']} | Chiudi a mano dall'app Pionex!", "pnl_pct": 0.0, "pnl_usdt": 0.0,
                    "current_r": 0.0, "alerts_fired": []}
        return _finalize_close(state, code, cfg, price, "🖐️ CHIUSURA MANUALE D'EMERGENZA", close, [], now_str, now_ts, capital_usdt)

    if command == "close":
        if not pos["is_open"]:
            return noop("Nessuna posizione registrata nel bot. Se hai coin sul conto usa '/sellall COIN' su Telegram o dalla Console.")
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
        close = live_sell({"mode": "LIVE_PIONEX", "amount_coin": 1e12, "invested_usdt": 0.0}, cfg, price, fraction=1.0)
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
            ev = evaluate_coin(COIN_MAP[coin], {}, 50, []) if coin in COIN_MAP else None
        if not ev:
            return noop(f"Analisi di '{coin}' non disponibile: impossibile calcolare SL/TP. Riprova.")
        rm = ev.get("risk_manager") or skill_budget_and_risk_manager(INVEST_EUR, eur_usd, ev["price"], ev["sl_price"], ev["tp_price"], state, False, coin, ev)
        usdt = usdt_req if usdt_req > 0 else rm["position_usdt"]
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


def trigger_self_chain_dispatch():
    """Se una posizione resta aperta a fine job, riavvia automaticamente il workflow GitHub per guardia continua."""
    if not SELF_CHAIN_WHEN_OPEN or not GITHUB_TOKEN or not GITHUB_REPO:
        return
    try:
        r = SESSION.post(
            f"https://api.github.com/repos/{GITHUB_REPO}/dispatches",
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
            json={"event_type": "tick"}, timeout=8
        )
        if r.status_code == 204:
            log("INFO", "Self-Chain Dispatch inviato: il prossimo ciclo partira' automaticamente per presidiare la posizione aperta.")
    except Exception as e:
        log("WARN", f"Self-Chain Dispatch non inviato: {e}")


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
                    "instruction": f"Il punteggio di {code} si sta indebolendo ({ev['final_score']}/10, FreqAI {ev['ai_prob'] * 100:.1f}%). Puoi chiudere anche scrivendo /close su Telegram."}
        if pnl >= SCALP_MIN_NET_PROFIT_PCT:
            return {"badge": "💰 POSIZIONE IN ZONA SCALP PROFIT (GUARDIA CONTINUA ATTIVA)", "color": "#10b981", "bg": "rgba(16,185,129,0.14)",
                    "instruction": f"La posizione su {code} e' in profitto netto ({pnl:+.2f}% / {curr_r:+.2f}R). Il Tick Guardian controlla il prezzo ogni {GUARDIAN_TICK_SEC}s per incassare al TP ({fmt_price(pos['tp_price'])}) o su inversione."}
        return {"badge": "✅ MANTIENI POSIZIONE (HOLD · TICK GUARDIAN ATTIVO)", "color": "#38bdf8", "bg": "rgba(56,189,248,0.14)",
                "instruction": f"Posizione aperta su {code} (PnL netto {pnl:+.2f}%). Protezione attiva con Stop-Loss a {fmt_price(pos['sl_price'])} e Take-Profit a {fmt_price(pos['tp_price'])}."}

    if trade_status.get("action_state") in ("ENTRY_FAILED", "CLOSE_FAILED"):
        return {"badge": "🚨 ERRORE ESECUZIONE ORDINE", "color": "#ef4444", "bg": "rgba(239,68,68,0.14)",
                "instruction": "Controlla subito saldi e ordini su Pionex: " + trade_status["detail"]}

    score, raw_100, code = best_eval["final_score"], best_eval["raw_100"], best_eval["code"]
    rm, tm = best_eval["risk_manager"], best_eval["scanner_timer"]
    if entry_conditions(best_eval):
        return {"badge": f"🚀 SEGNALE ATTIVO: APRI LONG SU {code}", "color": "#10b981", "bg": "rgba(16,185,129,0.15)",
                "instruction": f"Tutti i semafori sono VERDI su {code} ({score}/10 - {raw_100}/100). Budget consigliato {rm['position_usdt']:.2f} USDT, SL {fmt_price(best_eval['sl_price'])} e TP {fmt_price(best_eval['tp_price'])}."}
    if raw_100 >= 54:
        return {"badge": f"👀 QUASI PRONTO SU {code} ({raw_100}/100) - ATTENDI", "color": "#f59e0b", "bg": "rgba(245,158,11,0.14)",
                "instruction": f"{code} e' vicino alla soglia di ingresso ({raw_100}/100), ma manca una conferma ({tm['summary']} | {rm['status_str']})."}
    return {"badge": "🛡️ RESTA FLAT IN USDT (NON APRIRE ORA)", "color": "#94a3b8", "bg": "rgba(148,163,184,0.12)",
            "instruction": f"Nessuna delle 10 coin offre un vantaggio sufficiente ora (Migliore: {code} con {raw_100}/100 | {BTC_REGIME['label']})."}


def generate_webapp_html(ranked_evals, state, trade_status, fng_val, fng_class, session_str, eur_usd=1.08, macro_events=None):
    focus = next((e for e in ranked_evals if entry_conditions(e)), ranked_evals[0])
    advisor = build_manual_action_advisor(state, focus, {e["code"]: e for e in ranked_evals}, trade_status)
    now = datetime.now(timezone.utc)

    coins_payload = []
    for e in ranked_evals:
        rm = e.get("risk_manager") or skill_budget_and_risk_manager(INVEST_EUR, eur_usd, e["price"], e["sl_price"], e["tp_price"], state, e["high_soon"], e["code"], e)
        coins_payload.append({
            "code": e["code"], "name": e["coin"]["name"], "ws": e["coin"]["ws"], "kraken": e["coin"]["kraken"],
            "price": e["price"], "sl_price": e["sl_price"], "tp_price": e["tp_price"],
            "final_score": e["final_score"], "raw_100": e["raw_100"], "ai_prob": round(e["ai_prob"] * 100),
            "timer_summary": e["scanner_timer"]["summary"], "risk_ok": rm["passed"], "risk_status": rm["status_str"],
            "rr_ratio": round(rm["rr_ratio"], 2), "pos_usdt": round(rm["position_usdt"], 2), "entry_ok": entry_conditions(e)})

    macro = [{"title": ev["title"], "impact": ev["impact"], "ts": ev["ts"]} for ev in (macro_events or []) if "ts" in ev]
    payload = {
        "updated_ts": int(now.timestamp()), "session": session_str, "btc_regime": BTC_REGIME["label"],
        "fng_val": fng_val, "fng_class": fng_class, "mode": "LIVE" if pionex_live_enabled() else "SIMULAZIONE",
        "advisor": advisor, "waiting": trade_status["detail"] if trade_status.get("action_state") == "WAITING" else "",
        "position": state["position"], "stats": state["stats"], "history": state.get("history", []),
        "coins": coins_payload, "macro": macro, "macro_ok": bool(MACRO_STATUS.get("ok")),
        "repo": GITHUB_REPO, "workflow": "bot.yml", "ref": os.getenv("GITHUB_REF_NAME") or "main",
        "limits": {"max_trades": MAX_TRADES_PER_DAY, "daily_max_loss": DAILY_MAX_LOSS_PCT, "fee_rt": PIONEX_FEE_ROUNDTRIP_PCT}}
    payload_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    html = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Pionex FreqAI v10 Bot</title>
<style>
      :root { --bg:#0b1220; --card:#121a2b; --line:#22304a; --text:#f1f5f9; --muted:#94a3b8; --green:#10b981; --red:#ef4444; --amber:#f59e0b; --blue:#38bdf8; }
  * { box-sizing:border-box; margin:0; padding:0; font-family:system-ui,-apple-system,sans-serif; }
  body { background:var(--bg); color:var(--text); padding:14px; max-width:960px; margin:0 auto; line-height:1.45; }
  .top { display:flex; justify-content:space-between; align-items:center; gap:8px; flex-wrap:wrap; margin-bottom:12px; }
  .title { font-size:18px; font-weight:800; }
  .sub { font-size:12px; color:var(--muted); }
  .pill { font-size:11px; font-weight:700; padding:4px 9px; border-radius:999px; background:#1e293b; border:1px solid var(--line); }
  .banner { padding:14px; border-radius:12px; border:1px solid var(--line); margin-bottom:12px; }
  .banner-title { font-size:15px; font-weight:800; margin-bottom:4px; }
  .banner-sub { font-size:13px; color:#cbd5e1; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px; margin-bottom:12px; }
  .card-h { font-size:12px; font-weight:800; text-transform:uppercase; color:var(--muted); letter-spacing:0.6px; margin-bottom:10px; display:flex; justify-content:space-between; align-items:center; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:8px; }
  .kv { background:#0f172a; padding:10px; border-radius:8px; border:1px solid #1e293b; }
  .kv-l { font-size:11px; color:var(--muted); }
  .kv-v { font-size:15px; font-weight:800; margin-top:2px; font-variant-numeric:tabular-nums; }
  .tabs { display:flex; gap:6px; overflow-x:auto; padding-bottom:4px; margin-bottom:12px; }
  .tab { background:#162033; border:1px solid var(--line); color:var(--text); padding:8px 11px; border-radius:9px; cursor:pointer; min-width:86px; text-align:left; }
  .tab.active { border-color:var(--blue); background:#1a2b46; }
  .tab-top { display:flex; justify-content:space-between; font-size:12px; font-weight:800; }
  .tab-px { font-size:11px; color:var(--muted); margin-top:2px; font-variant-numeric:tabular-nums; }
  .bar { height:8px; background:#0f172a; border-radius:99px; overflow:hidden; margin:8px 0 4px; position:relative; border:1px solid #1e293b; }
  .bar > div { height:100%; transition:width .3s; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th, td { padding:7px 6px; border-bottom:1px solid #1e293b; text-align:left; font-variant-numeric:tabular-nums; }
  th { color:var(--muted); font-weight:700; font-size:11px; }
  .btn { background:#1e293b; color:var(--text); border:1px solid #334155; padding:8px 12px; border-radius:8px; font-size:12px; font-weight:700; cursor:pointer; }
  .btn:hover { filter:brightness(1.15); }
  .btn-red { background:rgba(239,68,68,.18); border-color:#ef4444; color:#fca5a5; }
  .btn-green { background:rgba(16,185,129,.18); border-color:#10b981; color:#6ee7b7; }
  input, select { background:#0f172a; color:var(--text); border:1px solid #334155; padding:7px 9px; border-radius:8px; font-size:12px; }
  .row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
  .muted { color:var(--muted); font-size:12px; }
  .pos-green { color:var(--green); } .pos-red { color:var(--red); } .pos-amber { color:var(--amber); }
</style>
</head>
<body>

<div class="top">
  <div>
    <div class="title">⚡ Pionex Freqtrade + FreqAI v10 Engine</div>
    <div class="sub" id="subline">Caricamento...</div>
  </div>
  <div class="row">
    <span class="pill" id="ws-pill">🔴 Prezzi Live...</span>
    <span class="pill" id="btc-pill">⚪ BTC</span>
    <span class="pill" id="mode-pill">MODO</span>
  </div>
</div>

<div class="banner" id="advisor">
  <div class="banner-title" id="adv-title"></div>
  <div class="banner-sub" id="adv-sub"></div>
</div>

<div class="card" id="pos-card"></div>

<div class="tabs" id="coin-tabs"></div>

<div class="card" id="coin-detail"></div>

<div class="card">
  <div class="card-h"><span>🎛️ Controlli Manuali (GitHub Actions / Telegram)</span><button class="btn" onclick="toggleCfg()">⚙️ Token GitHub</button></div>
  <div id="cfg-box" style="display:none; margin-bottom:10px;" class="row">
    <input id="gh-token" type="password" placeholder="GitHub Personal Access Token (scope: workflow)" style="flex:1;min-width:220px;">
    <button class="btn btn-green" onclick="saveToken()">Salva nel browser</button>
    <span class="muted">oppure invia direttamente <code>/status</code>, <code>/close</code>, <code>/open SOL</code>, <code>/sellall SOL</code> al bot Telegram.</span>
  </div>
  <div class="row">
    <button class="btn" onclick="dispatchCmd('status','',0)">🔄 Aggiorna Stato Ora</button>
    <button class="btn btn-red" onclick="dispatchCmd('close','',0)">🛑 Chiudi Posizione Registrata</button>
    <select id="man-coin"></select>
    <input id="man-usdt" type="number" step="1" placeholder="USDT (0 = auto)" style="width:120px;">
    <button class="btn btn-green" onclick="manualOpen()">🚀 Apri Long Manuale</button>
    <button class="btn btn-red" onclick="manualSellAll()">🧹 Vendi Tutto Saldo Coin</button>
  </div>
  <div id="cmd-msg" class="muted" style="margin-top:8px;"></div>
</div>

<div class="card">
  <div class="card-h"><span>📊 Statistiche & Protezioni Giornaliere</span><span id="daily-tag"></span></div>
  <div class="grid" id="stats-grid"></div>
</div>

<div class="card">
  <div class="card-h"><span>📜 Storico Operazioni Recenti</span><span id="macro-tag"></span></div>
  <div id="hist-box"></div>
</div>

<script>
const D = __PAYLOAD_JSON__;
const livePx = {};
let selected = (D.position && D.position.is_open) ? D.position.coin_code : (D.coins[0]?.code || 'SOL');

function fmtPx(p){
  p = Number(p||0);
  if(p<=0) return '$0.00';
  if(p>=1000) return '$'+p.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
  if(p>=10) return '$'+p.toFixed(2);
  if(p>=1) return '$'+p.toFixed(3);
  return '$'+p.toFixed(4);
}
function ageStr(ts){
  const s = Math.max(0, Math.floor(Date.now()/1000 - ts));
  if(s<60) return s+'s fa';
  if(s<3600) return Math.floor(s/60)+'m fa';
  return (s/3600).toFixed(1)+'h fa';
}

function renderTop(){
  document.getElementById('subline').textContent =
    `Ultimo calcolo AI: ${ageStr(D.updated_ts)} · ${D.session} · Fear&Greed: ${D.fng_val} (${D.fng_class})`;
  const mp = document.getElementById('mode-pill');
  mp.textContent = D.mode === 'LIVE' ? '🟢 LIVE PIONEX' : '🧪 SIMULAZIONE';
  mp.style.borderColor = D.mode === 'LIVE' ? '#10b981' : '#f59e0b';
  const bp = document.getElementById('btc-pill');
  bp.textContent = D.btc_regime || '⚪ BTC';

  const adv = document.getElementById('advisor');
  adv.style.background = D.advisor.bg;
  adv.style.borderColor = D.advisor.color;
  const t = document.getElementById('adv-title');
  t.style.color = D.advisor.color;
  t.textContent = D.advisor.badge;
  document.getElementById('adv-sub').textContent = D.advisor.instruction + (D.waiting ? ' — ' + D.waiting : '');
}

function renderPosition(){
  const box = document.getElementById('pos-card');
  const p = D.position;
  if(!p || !p.is_open){
    box.innerHTML = `<div class="card-h"><span>💼 Posizione Attiva</span><span>FLAT (100% USDT)</span></div>
      <div class="muted">${D.waiting || 'Nessuna posizione aperta. Il motore FreqAI v10 attende un setup ad alta probabilita\\'.'}</div>`;
    return;
  }
  const coin = D.coins.find(c=>c.code===p.coin_code) || {};
  const cur = livePx[p.coin_code] || coin.price || p.entry_price;
  const fee = (D.limits.fee_rt || 0.10) / 100;
  const partialPnl = Number(p.realized_partial_usdt || 0);
  const netPct = (((cur - p.entry_price) / p.entry_price) - fee) * 100;
  const netUsdt = p.invested_usdt * (netPct / 100) + partialPnl;
  const oneR = p.one_r || Math.max(p.entry_price - p.initial_sl, p.entry_price*0.01);
  const rMult = (cur - p.entry_price) / oneR;
  const span = Math.max(p.tp_price - p.sl_price, 1e-9);
  const prog = Math.max(0, Math.min(100, ((cur - p.sl_price) / span) * 100));
  const col = netUsdt >= 0 ? 'pos-green' : 'pos-red';
  const scaleTag = p.partial_tp_done ? ` · 💰 Scale-Out 50% (+${partialPnl.toFixed(2)} USDT)` : '';

  box.innerHTML = `
    <div class="card-h"><span>🔥 POSIZIONE APERTA: ${p.coin_code} (${p.mode}${scaleTag})</span><span>Aperta: ${p.opened_at}</span></div>
    <div class="grid">
      <div class="kv"><div class="kv-l">Prezzo Ingresso</div><div class="kv-v">${fmtPx(p.entry_price)}</div></div>
      <div class="kv"><div class="kv-l">Prezzo Live</div><div class="kv-v">${fmtPx(cur)}</div></div>
      <div class="kv"><div class="kv-l">PnL Netto Totale</div><div class="kv-v ${col}">${netPct>=0?'+':''}${netPct.toFixed(2)}% (${netUsdt>=0?'+':''}${netUsdt.toFixed(2)} $)</div></div>
      <div class="kv"><div class="kv-l">Multiplo R</div><div class="kv-v ${col}">${rMult>=0?'+':''}${rMult.toFixed(2)}R</div></div>
      <div class="kv"><div class="kv-l">Stop-Loss ${p.breakeven_locked?'🔒':''}</div><div class="kv-v pos-red">${fmtPx(p.sl_price)}</div></div>
      <div class="kv"><div class="kv-l">Take-Profit 🎯</div><div class="kv-v pos-green">${fmtPx(p.tp_price)}</div></div>
    </div>
    <div class="bar"><div style="width:${prog}%; background:${netUsdt>=0?'#10b981':'#ef4444'}"></div></div>
    <div class="row" style="justify-content:space-between"><span class="muted">SL ${fmtPx(p.sl_price)}</span><span class="muted">Investito residuo: ${Number(p.invested_usdt).toFixed(2)} USDT (${p.amount_coin} ${p.coin_code})</span><span class="muted">TP ${fmtPx(p.tp_price)}</span></div>`;
}

function renderTabs(){
  const t = document.getElementById('coin-tabs');
  t.innerHTML = D.coins.map(c=>{
    const px = livePx[c.code] || c.price;
    const badge = c.entry_ok ? '🟢' : (c.raw_100>=54 ? '🟡' : '⚪');
    return `<button class="tab ${c.code===selected?'active':''}" onclick="selCoin('${c.code}')">
      <div class="tab-top"><span>${badge} ${c.code}</span><span>${c.raw_100}/100</span></div>
      <div class="tab-px" id="tabpx-${c.code}">${fmtPx(px)}</div>
    </button>`;
  }).join('');
}

function renderDetail(){
  const c = D.coins.find(x=>x.code===selected) || D.coins[0];
  if(!c) return;
  const px = livePx[c.code] || c.price;
  document.getElementById('coin-detail').innerHTML = `
    <div class="card-h"><span>🔍 Dettaglio ${c.name} (${c.code}) — Score ${c.final_score}/10 (${c.raw_100}/100)</span><span>${c.entry_ok?'🟢 SEGNALE PRONTO':'⏳ IN ATTESA'}</span></div>
    <div class="grid">
      <div class="kv"><div class="kv-l">Prezzo Live</div><div class="kv-v">${fmtPx(px)}</div></div>
      <div class="kv"><div class="kv-l">FreqAI Triple-Barrier</div><div class="kv-v">${c.ai_prob}%</div></div>
      <div class="kv"><div class="kv-l">Stop-Loss Calcolato</div><div class="kv-v pos-red">${fmtPx(c.sl_price)}</div></div>
      <div class="kv"><div class="kv-l">Take-Profit Calcolato</div><div class="kv-v pos-green">${fmtPx(c.tp_price)}</div></div>
      <div class="kv"><div class="kv-l">Reward / Risk Netto</div><div class="kv-v">${c.rr_ratio}x</div></div>
      <div class="kv"><div class="kv-l">Size Consigliata</div><div class="kv-v">${c.pos_usdt.toFixed(2)} USDT</div></div>
    </div>
    <div class="muted" style="margin-top:8px;">⏱️ <b>Timer 15m:</b> ${c.timer_summary} &nbsp;|&nbsp; 🛡️ <b>Rischio:</b> ${c.risk_status}</div>`;
}

function renderStats(){
  const s = D.stats || {};
  const wr = s.total_trades ? ((s.wins/s.total_trades)*100).toFixed(0)+'%' : 'N/A';
  document.getElementById('daily-tag').textContent = `Oggi: ${s.daily_trades||0}/${D.limits.max_trades} trade · PnL oggi: ${(s.daily_pnl_pct||0)>=0?'+':''}${(s.daily_pnl_pct||0).toFixed(2)}%`;
  document.getElementById('stats-grid').innerHTML = `
    <div class="kv"><div class="kv-l">Trade Totali (W/L)</div><div class="kv-v">${s.total_trades||0} (${s.wins||0}W / ${s.losses||0}L)</div></div>
    <div class="kv"><div class="kv-l">Win Rate</div><div class="kv-v">${wr}</div></div>
    <div class="kv"><div class="kv-l">PnL Cumulato USDT</div><div class="kv-v ${(s.total_pnl_usdt||0)>=0?'pos-green':'pos-red'}">${(s.total_pnl_usdt||0)>=0?'+':''}${(s.total_pnl_usdt||0).toFixed(2)} $</div></div>
    <div class="kv"><div class="kv-l">Ultima Azione</div><div class="kv-v" style="font-size:12px;">${s.last_action||'—'}</div></div>`;

  const h = D.history || [];
  document.getElementById('macro-tag').textContent = D.macro_ok ? `Calendario USD: ${(D.macro||[]).length} eventi` : 'Calendario USD offline';
  document.getElementById('hist-box').innerHTML = h.length ? `<table><thead><tr><th>Coin</th><th>Ingresso</th><th>Uscita</th><th>PnL</th><th>Motivo</th><th>Data</th></tr></thead><tbody>` +
    h.slice(0,10).map(r=>`<tr><td><b>${r.coin}</b></td><td>${fmtPx(r.entry)}</td><td>${fmtPx(r.exit)}</td><td class="${r.pnl_usdt>=0?'pos-green':'pos-red'}">${r.pnl_pct>=0?'+':''}${r.pnl_pct}% (${r.pnl_usdt>=0?'+':''}${r.pnl_usdt}$)</td><td>${r.reason}</td><td>${r.closed_at}</td></tr>`).join('') +
    `</tbody></table>` : `<div class="muted">Nessun trade chiuso finora.</div>`;
}

function selCoin(code){ selected = code; renderTabs(); renderDetail(); }
function toggleCfg(){ const b=document.getElementById('cfg-box'); b.style.display=b.style.display==='none'?'flex':'none'; }
function saveToken(){ localStorage.setItem('gh_pat', document.getElementById('gh-token').value.trim()); document.getElementById('cmd-msg').textContent='✅ Token salvato nel browser.'; }

async function dispatchCmd(command, coin, amount_usdt){
  const tok = localStorage.getItem('gh_pat') || document.getElementById('gh-token').value.trim();
  const msg = document.getElementById('cmd-msg');
  if(!tok){ toggleCfg(); msg.textContent='⚠️ Inserisci prima un GitHub Personal Access Token (scope: workflow) oppure invia il comando su Telegram.'; return; }
  if(!D.repo){ msg.textContent='⚠️ Repository GitHub non rilevato.'; return; }
  msg.textContent = `⏳ Invio comando '${command}' a GitHub Actions...`;
  try{
    const r = await fetch(`https://api.github.com/repos/${D.repo}/actions/workflows/${D.workflow}/dispatches`, {
      method:'POST',
      headers:{'Authorization':`Bearer ${tok}`,'Accept':'application/vnd.github+json','Content-Type':'application/json'},
      body: JSON.stringify({ref: D.ref || 'main', inputs:{command, coin: String(coin||''), amount_usdt: String(amount_usdt||0)}})
    });
    msg.textContent = r.status===204 ? `✅ Comando '${command}' avviato! Riceverai conferma su Telegram tra ~40s.` : `❌ Errore GitHub API HTTP ${r.status}`;
  }catch(e){ msg.textContent = '❌ Errore rete: '+e; }
}
function manualOpen(){ dispatchCmd('open', document.getElementById('man-coin').value, document.getElementById('man-usdt').value||0); }
function manualSellAll(){
  const c = document.getElementById('man-coin').value;
  if(confirm(`Confermi la vendita immediata a mercato di TUTTO il saldo ${c} su Pionex?`)) dispatchCmd('sell_all', c, 0);
}

function connectWS(){
  try{
    const ws = new WebSocket('wss://ws.kraken.com');
    const pairToCode = {};
    D.coins.forEach(c => { pairToCode[c.ws] = c.code; });
    ws.onopen = () => {
      document.getElementById('ws-pill').textContent = '🟢 Live Kraken WS';
      ws.send(JSON.stringify({event:'subscribe', pair: D.coins.map(c=>c.ws), subscription:{name:'ticker'}}));
    };
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if(Array.isArray(m) && m[1] && m[1].c){
        const code = pairToCode[m[3]];
        const px = parseFloat(m[1].c[0]);
        if(code && px>0){
          livePx[code] = px;
          const el = document.getElementById('tabpx-'+code);
          if(el) el.textContent = fmtPx(px);
          if(code===selected) renderDetail();
          if(D.position && D.position.is_open && D.position.coin_code===code) renderPosition();
        }
      }
    };
    ws.onclose = () => { document.getElementById('ws-pill').textContent = '🟡 Riconnessione WS...'; setTimeout(connectWS, 4000); };
  }catch(e){}
}

(function init(){
  document.getElementById('gh-token').value = localStorage.getItem('gh_pat') || '';
  const sel = document.getElementById('man-coin');
  sel.innerHTML = D.coins.map(c=>`<option value="${c.code}">${c.code}</option>`).join('');
  renderTop(); renderPosition(); renderTabs(); renderDetail(); renderStats();
  setInterval(renderTop, 10000);
  connectWS();
})();
</script>
</body>
</html>"""

    html = html.replace("__PAYLOAD_JSON__", payload_json)
    try:
        with open(WEBAPP_FILE, "w", encoding="utf-8") as f:
            f.write(html)
        log("INFO", f"Web Console {WEBAPP_FILE} aggiornata con successo.")
    except Exception as e:
        log("ERROR", f"Impossibile scrivere {WEBAPP_FILE}: {e}")


# ==============================================================================
# 12. REPORTISTICA E NOTIFICHE ISTANTANEE TELEGRAM
# ==============================================================================
def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("WARN", "Credenziali Telegram non configurate: salto invio messaggio.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = SESSION.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            log("WARN", f"Telegram API HTTP {r.status_code}: {r.text[:160]}")
    except Exception as e:
        log("WARN", f"Errore invio Telegram: {e}")


def build_telegram_report(ranked_evals, state, trade_status, fng_val, fng_class, session_str, eur_usd):
    pos, stats = state["position"], state["stats"]
    best = ranked_evals[0]
    mode_str = "🟢 LIVE PIONEX" if pionex_live_enabled() else "🧪 SIMULAZIONE"
    lines = [
        f"🤖 <b>PIONEX FREQTRADE + FREQAI v10</b> ({mode_str})",
        f"🌍 {html_lib.escape(session_str)} | {html_lib.escape(BTC_REGIME['label'])} | F&G: <b>{fng_val}</b> ({html_lib.escape(fng_class)})",
        "",
        f"<b>{html_lib.escape(trade_status['headline'])}</b>",
        f"{html_lib.escape(trade_status['detail'])}",
    ]

    for al in trade_status.get("alerts_fired", []):
        lines.append(f"• {html_lib.escape(al)}")

    if pos["is_open"]:
        scale_info = f" | Scale-Out 50%: +{pos.get('realized_partial_usdt', 0.0):.2f}$" if pos.get("partial_tp_done") else ""
        lines.extend([
            "",
            f"📌 <b>POSIZIONE ATTIVA: {pos['coin_code']}</b> ({pos['mode']}{scale_info})",
            f"• Entry: <code>{fmt_price(pos['entry_price'])}</code> | SL: <code>{fmt_price(pos['sl_price'])}</code> | TP: <code>{fmt_price(pos['tp_price'])}</code>",
            f"• Size residua: <code>{pos['amount_coin']} {pos['coin_code']}</code> ({pos['invested_usdt']:.2f} USDT)",
        ])

    lines.extend([
        "",
        "🏆 <b>TOP 5 CLASSIFICA FREQAI TRIPLE-BARRIER (10 COIN):</b>",
    ])
    for i, ev in enumerate(ranked_evals[:5], 1):
        flag = "🟢" if entry_conditions(ev) else ("🟡" if ev["raw_100"] >= 54 else "⚪")
        di_tag = "OK" if ev.get("do_predict", 1) == 1 else "OUTLIER"
        lines.append(
            f"{i}. {flag} <b>{ev['code']}</b> ({fmt_price(ev['price'])}) — "
            f"Score: <b>{ev['final_score']}/10</b> ({ev['raw_100']}/100) | "
            f"AI: <b>{ev['ai_prob']*100:.0f}%</b> (DI:{di_tag}) | 15m: {ev['scanner_timer']['triggers']}/4"
        )

    wr = (stats["wins"] / stats["total_trades"] * 100.0) if stats["total_trades"] > 0 else 0.0
    lines.extend([
        "",
        f"📊 <b>Statistiche:</b> {stats['total_trades']} trade ({stats['wins']}W/{stats['losses']}L, WR {wr:.0f}%) | "
        f"PnL Tot: <b>{stats['total_pnl_usdt']:+.2f} USDT</b> | Oggi: <b>{stats.get('daily_pnl_pct', 0.0):+.2f}%</b>",
        f"💬 <i>Comandi rapidi chat: /status · /close · /open {best['code']} · /sellall {best['code']}</i>",
    ])
    return "\n".join(lines)


# ==============================================================================
# 13. ESECUZIONE CICLO COMPLETO + MODALITA' CONTINUA (TICK GUARDIAN)
# ==============================================================================
def run_full_cycle(state, manual_override=None):
    """Esegue una scansione completa multi-timeframe + FreqAI su tutte le 10 coin."""
    LIVE_PX.clear()
    LIVE_PX.update(fetch_pionex_prices())
    eur_usd = fetch_eur_usd_rate()
    futures_map = fetch_all_kraken_futures()
    fng_val, fng_class = fetch_fear_and_greed()
    macro_events = fetch_macro_calendar()
    session_str = skill_nyse_bell_and_session()

    all_evals = scan_all_coins(COINS, futures_map, fng_val, macro_events)
    pos = state["position"]
    if pos["is_open"] and pos["coin_code"] not in all_evals:
        oc = pos["coin_code"]
        all_evals[oc] = make_fallback_eval(COIN_MAP.get(oc, COINS[0]), pos)

    if not all_evals:
        log("ERROR", "Nessuna coin analizzata con successo (API Kraken non raggiungibili).")
        return None

    for code, ev in all_evals.items():
        ev["risk_manager"] = skill_budget_and_risk_manager(
            INVEST_EUR, eur_usd, ev["price"], ev["sl_price"], ev["tp_price"], state, ev["high_soon"], code, ev
        )
        ev["exit_plan"] = skill_exit_planner(ev["price"], ev["sl_price"], ev["tp_price"])

    ranked_evals = sorted(all_evals.values(), key=lambda x: (x["raw_100"], x["ai_prob"]), reverse=True)

    # Controlla se c'e' un comando manuale da GitHub Actions o da Telegram
    tg_cmd = poll_telegram_commands(state)
    cmd_obj = manual_override or tg_cmd

    if cmd_obj and cmd_obj.get("command") in ("close", "sell_all", "open"):
        trade_status = run_manual_command(
            state, all_evals, eur_usd, cmd_obj["command"], cmd_obj.get("coin", ""), cmd_obj.get("usdt", 0.0)
        )
    else:
        trade_status = manage_autonomous_trading(state, ranked_evals, all_evals, eur_usd)

    generate_webapp_html(ranked_evals, state, trade_status, fng_val, fng_class, session_str, eur_usd, macro_events)
    report = build_telegram_report(ranked_evals, state, trade_status, fng_val, fng_class, session_str, eur_usd)
    send_telegram_message(report)

    return {
        "ranked_evals": ranked_evals,
        "all_evals": all_evals,
        "eur_usd": eur_usd,
        "trade_status": trade_status,
        "fng_val": fng_val,
        "fng_class": fng_class,
        "session_str": session_str,
        "macro_events": macro_events,
    }


def run_continuous_guardian(state, ctx):
    """
    Tick Guardian Real-Time:
    - Se c'e' una posizione aperta, monitora i prezzi live Pionex e i comandi Telegram ogni `GUARDIAN_TICK_SEC` (15s)
      per applicare all'istante Break-Even, Scale-Out 50%, Trailing Stop e Take-Profit.
    - Se una coin e' 'HOT' (raw_100 >= 54), monitora ogni 15s e ricalcola le candele ogni 5 min per cogliere il breakout.
    - In DAEMON_MODE=true gira all'infinito 24/7.
    """
    pos = state["position"]
    best = ctx["ranked_evals"][0] if ctx["ranked_evals"] else None
    is_hot = bool(best and best["raw_100"] >= 54 and best["risk_manager"]["passed"])

    if DAEMON_MODE:
        max_seconds = 10 ** 9
        log("INFO", f"DAEMON_MODE=true attivo: avvio loop infinito 24/7 (tick ogni {GUARDIAN_TICK_SEC}s, rescan AI ogni {GUARDIAN_RESCAN_SEC}s).")
    elif pos["is_open"]:
        max_seconds = int(GUARDIAN_MINUTES_OPEN * 60)
        log("INFO", f"Posizione aperta su {pos['coin_code']}: avvio Tick Guardian continuo per {GUARDIAN_MINUTES_OPEN:.1f} minuti (tick ogni {GUARDIAN_TICK_SEC}s).")
    elif is_hot:
        max_seconds = int(GUARDIAN_MINUTES_HOT * 60)
        log("INFO", f"Setup quasi pronto su {best['code']} ({best['raw_100']}/100): avvio Tick Guardian per {GUARDIAN_MINUTES_HOT:.1f} minuti.")
    else:
        return

    start_mon = time.monotonic()
    last_rescan = time.monotonic()

    while time.monotonic() - start_mon < max_seconds:
        time.sleep(max(5, GUARDIAN_TICK_SEC))
        now_dt = datetime.now(timezone.utc)
        now_str, now_ts = now_dt.strftime("%d/%m %H:%M UTC"), int(now_dt.timestamp())

        # 1. Aggiorna i ticker istantanei Pionex (1 chiamata leggera)
        px_now = fetch_pionex_prices()
        if px_now:
            LIVE_PX.update(px_now)

        # 2. Controlla comandi diretti inviati su Telegram (/status, /close, /open, /sellall)
        tg_cmd = poll_telegram_commands(state)
        if tg_cmd:
            c_type = tg_cmd["command"]
            log("INFO", f"Comando Telegram ricevuto nel Tick Guardian: {tg_cmd}")
            if c_type == "status":
                rep = build_telegram_report(ctx["ranked_evals"], state, ctx["trade_status"], ctx["fng_val"], ctx["fng_class"], ctx["session_str"], ctx["eur_usd"])
                send_telegram_message(rep)
            else:
                st = run_manual_command(state, ctx["all_evals"], ctx["eur_usd"], c_type, tg_cmd.get("coin", ""), tg_cmd.get("usdt", 0.0))
                ctx["trade_status"] = st
                send_telegram_message(f"<b>{html_lib.escape(st['headline'])}</b>\n{html_lib.escape(st['detail'])}")
                generate_webapp_html(ctx["ranked_evals"], state, st, ctx["fng_val"], ctx["fng_class"], ctx["session_str"], ctx["eur_usd"], ctx["macro_events"])
                if not state["position"]["is_open"] and not DAEMON_MODE:
                    break

        # 3. Se c'e' una posizione aperta, verifica ogni 15s SL / TP / Scale-Out 50% / Break-Even / Trailing
        if state["position"]["is_open"]:
            capital_usdt = INVEST_EUR * ctx["eur_usd"]
            st = _manage_open_position(state, ctx["all_evals"], capital_usdt, now_str, now_ts)
            ctx["trade_status"] = st

            if st.get("alerts_fired"):
                for al in st["alerts_fired"]:
                    send_telegram_message(f"⚡ <b>TICK GUARDIAN ({state['position']['coin_code']})</b>\n{html_lib.escape(al)}")

            if st["action_state"] in ("CLOSED_NOW", "CLOSE_FAILED"):
                send_telegram_message(f"<b>{html_lib.escape(st['headline'])}</b>\n{html_lib.escape(st['detail'])}")
                generate_webapp_html(ctx["ranked_evals"], state, st, ctx["fng_val"], ctx["fng_class"], ctx["session_str"], ctx["eur_usd"], ctx["macro_events"])
                if st["action_state"] == "CLOSED_NOW" and not DAEMON_MODE:
                    log("INFO", "Posizione chiusa durante il Tick Guardian: termino il job per salvare subito lo stato.")
                    break

        # 4. Ogni GUARDIAN_RESCAN_SEC (es. 5 minuti), rianalizza candele 15m/1h e FreqAI
        if time.monotonic() - last_rescan >= GUARDIAN_RESCAN_SEC:
            last_rescan = time.monotonic()
            log("INFO", "Tick Guardian: avvio rianalisi periodica 15m/1h + FreqAI...")
            new_ctx = run_full_cycle(state)
            if new_ctx:
                ctx = new_ctx
                if state["position"]["is_open"] and not DAEMON_MODE:
                    # Estende la guardia se si e' appena aperta una posizione
                    max_seconds = max(max_seconds, int(GUARDIAN_MINUTES_OPEN * 60))


def main():
    log("INFO", f"Avvio Pionex Freqtrade + FreqAI Engine v10.0 (Evento: {GITHUB_EVENT_NAME}, AutoTrade={AUTO_TRADE_ENABLED}, Live={pionex_live_enabled()})")
    state = load_state()

    manual_override = None
    if MANUAL_COMMAND in ("close", "sell_all", "open", "status"):
        manual_override = {"command": MANUAL_COMMAND, "coin": MANUAL_COIN, "usdt": MANUAL_USDT}

    ctx = run_full_cycle(state, manual_override=manual_override)
    if not ctx:
        return

    # Avvia il Tick Guardian continuo (se trade aperto, coin hot o DAEMON_MODE=true)
    if MANUAL_COMMAND not in ("close", "sell_all"):
        run_continuous_guardian(state, ctx)

    # Se a fine ciclo la posizione e' ancora aperta su GitHub Actions, rilancia il workflow per guardia senza buchi
    if state["position"]["is_open"] and not DAEMON_MODE:
        trigger_self_chain_dispatch()


if __name__ == "__main__":
    main()
