import os
import re
import sys
import json
import gzip
import time
import math
import hmac
import hashlib
import random
import warnings
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

# --- ML opzionale: se scikit-learn manca il bot funziona lo stesso, con il filtro ML spento ---
try:
    import numpy as np
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.neural_network import MLPClassifier
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import roc_auc_score
    ML_OK = True
    warnings.filterwarnings("ignore")
except Exception:  # pragma: no cover
    ML_OK = False

STRAT_VER = "v12"


# ==============================================================================
# 0. CONFIGURAZIONE
# ==============================================================================
def _env(name, default):
    v = os.getenv(name)
    return v.strip() if v is not None and v.strip() != "" else str(default)


def _envf(name, default):
    try:
        return float(_env(name, default))
    except ValueError:
        return float(default)


def _envi(name, default):
    return int(_envf(name, default))


def _envb(name, default):
    return _env(name, default).lower() in ("1", "true", "yes", "on", "y")


TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
                  or os.getenv("BOT_TOKEN") or os.getenv("TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or os.getenv("TELEGRAM_CHAT") or "").strip()
PIONEX_API_KEY = (os.getenv("PIONEX_API_KEY") or os.getenv("API_KEY") or "").strip()
PIONEX_API_SECRET = (os.getenv("PIONEX_API_SECRET") or os.getenv("API_SECRET") or "").strip()
GITHUB_TOKEN = (os.getenv("GH_PAT") or os.getenv("GITHUB_TOKEN") or "").strip()
GITHUB_REPO = _env("GITHUB_REPOSITORY", "")
GITHUB_EVENT_NAME = _env("GITHUB_EVENT_NAME", "manual")
ANTHROPIC_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip()

MANUAL_COMMAND = _env("MANUAL_COMMAND", "").lower()
MANUAL_COIN = _env("MANUAL_COIN", "").upper()
MANUAL_USDT = _envf("MANUAL_USDT", 0)

INVEST_EUR = _envf("INVEST_EUR", 50.0)
AUTO_TRADE_ENABLED = _envb("AUTO_TRADE_ENABLED", "false")
DAEMON_MODE = _envb("DAEMON_MODE", "false")
ALWAYS_ON_24_7 = _envb("ALWAYS_ON_24_7", "true")
GUARDIAN_MINUTES = _envf("GUARDIAN_MINUTES_OPEN", 18.0)
GUARDIAN_TICK_SEC = _envi("GUARDIAN_TICK_SEC", 15)
GUARDIAN_RESCAN_SEC = _envi("GUARDIAN_RESCAN_SEC", 300)
SELF_CHAIN_WHEN_OPEN = _envb("SELF_CHAIN_WHEN_OPEN", "true")
TELEGRAM_ROUTINE_MIN = _envi("TELEGRAM_ROUTINE_MIN", 30)

# --- dati storici (365g da Binance vision + cache incrementale compressa) ---
HIST_DAYS = _envi("HIST_DAYS", 365)
HIST_CACHE = _env("HIST_CACHE", "hist_cache.json.gz")

# --- costi (percentuali per lato) ---
FEE_SIDE = _envf("FEE_PCT_PER_SIDE", 0.05)
SLIP_SIDE = _envf("SLIPPAGE_PCT_PER_SIDE", 0.05)
COST_RT = 2.0 * (FEE_SIDE + SLIP_SIDE)

# --- piano di uscita (calibrato per evitare stop a zero sui retest) ---
ATR_STOP_K = _envf("ATR_STOP_K", 1.6)
MIN_STOP_PCT = _envf("MIN_STOP_PCT", 1.0)
MAX_STOP_PCT = _envf("MAX_STOP_PCT", 3.0)
TARGET_R = _envf("TARGET_R", 2.0)
MIN_RR_NET = _envf("MIN_RR_NET", 1.4)
BE_AT_R = _envf("BE_AT_R", 1.20)
TRAIL_START_R = _envf("TRAIL_START_R", 1.55)
TRAIL_R = _envf("TRAIL_R", 0.85)
MAX_HOLD_H = _envi("MAX_HOLD_HOURS", 16)

# --- setup / filtri ---
MAX_EXT_ATR = _envf("MAX_EXTENSION_ATR", 0.65)
MAX_PUMP_24H_PCT = _envf("MAX_PUMP_24H_PCT", 12.0)
MAX_SPREAD_BPS = _envf("MAX_SPREAD_BPS", 20.0)
FUNDING_MAX_PCT = _envf("FUNDING_MAX_8H_PCT", 0.04)
SETUP_VR_MIN = _envf("SETUP_VR_MIN", 1.0)
SETUP_ADX_MIN = _envf("SETUP_ADX_MIN", 16.0)

# --- edge (Time Machine) ---
MIN_POOL_N = _envi("MIN_POOL_N", 25)
MIN_SETUP_N = _envi("MIN_SETUP_N", 15)
MIN_EXP_R = _envf("MIN_EXP_R", 0.06)
SHRINK_K = _envf("SHRINK_K", 20.0)
RECENT_DAYS = _envi("RECENT_DAYS", 90)
RECENT_MIN_N = _envi("RECENT_MIN_N", 15)
RECENT_MIN_EXP_R = _envf("RECENT_MIN_EXP_R", -0.15)
MC_PATHS = _envi("MC_PATHS", 1500)
MC_MIN_P_TARGET = _envf("MC_MIN_P_TARGET", 0.18)

# --- ML (meta-labeling con ensemble LogReg + HistGradientBoosting + MLP, walk-forward purged) ---
ML_ENABLED = _envb("ML_ENABLED", "true")
ML_MIN_EVENTS = _envi("ML_MIN_EVENTS", 150)
ML_SKIP_QUANTILE = _envf("ML_SKIP_QUANTILE", 0.40)
ML_MIN_AUC = _envf("ML_MIN_AUC", 0.54)
RISK_SCALE_BY_ML = _envb("RISK_SCALE_BY_ML", "true")

# --- rischio ---
RISK_PER_TRADE_PCT = _envf("RISK_PER_TRADE_PCT", 1.0)
DAILY_MAX_LOSS_PCT = _envf("DAILY_MAX_LOSS_PCT", 3.0)
MAX_TRADES_PER_DAY = _envi("MAX_TRADES_PER_DAY", 4)
MAX_CONSEC_LOSSES = _envi("MAX_CONSEC_LOSSES", 3)
CASH_BUFFER_PCT = _envf("CASH_BUFFER_PCT", 5.0)
MIN_ORDER_USDT = _envf("MIN_ORDER_USDT", 10.5)
ENTRY_START_UTC = _envi("ENTRY_START_UTC", 6)
ENTRY_END_UTC = _envi("ENTRY_END_UTC", 23)
MIN_MINUTES_BETWEEN_TRADES = _envi("MIN_MINUTES_BETWEEN_TRADES", 60)
COOLDOWN_AFTER_LOSS_MIN = _envi("COOLDOWN_AFTER_LOSS_MIN", 180)
SAME_COIN_COOLDOWN_H = _envf("SAME_COIN_COOLDOWN_H", 6.0)
ENTRY_SCORE_MIN = _envi("ENTRY_SCORE_MIN", 35)
MIN_ORDER_FLOOR = MIN_ORDER_USDT

# --- passaggio al live ---
LIVE_PROOF_REQUIRED = _envb("LIVE_PROOF_REQUIRED", "true")
PROOF_MIN_TRADES = _envi("PROOF_MIN_TRADES", 20)
PROOF_MIN_EXP_R = _envf("PROOF_MIN_EXP_R", 0.10)
REQUIRE_APPROVAL = _envb("REQUIRE_APPROVAL", "false")
PENDING_TTL_MIN = _envi("PENDING_TTL_MIN", 10)

# --- revisore Claude (opzionale: veto + risk_mult) ---
CLAUDE_REVIEW = _envb("CLAUDE_REVIEW_ENABLED", "false")
CLAUDE_MODEL = _env("CLAUDE_MODEL", "claude-haiku-5-5")

STATE_FILE = "bot_state.json"
SNAPSHOT_FILE = "bot_snapshot.json"
HTTP_TIMEOUT = 12
PIONEX_BASE = "https://api.pionex.com"
BINANCE_HOSTS = ("https://data-api.binance.vision", "https://api.binance.com")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PionexEdgeBot/12.0)"}

COINS = [
    {"code": "SOL",  "name": "Solana",    "kraken": "SOLUSD",  "binance": "SOLUSDT",  "futures": "PF_SOLUSD",  "pionex": "SOL_USDT",  "prec": 3},
    {"code": "BTC",  "name": "Bitcoin",   "kraken": "XBTUSD",  "binance": "BTCUSDT",  "futures": "PF_XBTUSD",  "pionex": "BTC_USDT",  "prec": 5},
    {"code": "ETH",  "name": "Ethereum",  "kraken": "ETHUSD",  "binance": "ETHUSDT",  "futures": "PF_ETHUSD",  "pionex": "ETH_USDT",  "prec": 4},
    {"code": "XRP",  "name": "Ripple",    "kraken": "XRPUSD",  "binance": "XRPUSDT",  "futures": "PF_XRPUSD",  "pionex": "XRP_USDT",  "prec": 1},
    {"code": "SUI",  "name": "Sui",       "kraken": "SUIUSD",  "binance": "SUIUSDT",  "futures": "PF_SUIUSD",  "pionex": "SUI_USDT",  "prec": 2},
    {"code": "DOGE", "name": "Dogecoin",  "kraken": "XDGUSD",  "binance": "DOGEUSDT", "futures": "PF_DOGEUSD", "pionex": "DOGE_USDT", "prec": 0},
    {"code": "ADA",  "name": "Cardano",   "kraken": "ADAUSD",  "binance": "ADAUSDT",  "futures": "PF_ADAUSD",  "pionex": "ADA_USDT",  "prec": 1},
    {"code": "AVAX", "name": "Avalanche", "kraken": "AVAXUSD", "binance": "AVAXUSDT", "futures": "PF_AVAXUSD", "pionex": "AVAX_USDT", "prec": 2},
    {"code": "LINK", "name": "Chainlink", "kraken": "LINKUSD", "binance": "LINKUSDT", "futures": "PF_LINKUSD", "pionex": "LINK_USDT", "prec": 2},
    {"code": "DOT",  "name": "Polkadot",  "kraken": "DOTUSD",  "binance": "DOTUSDT",  "futures": "PF_DOTUSD",  "pionex": "DOT_USDT",  "prec": 2},
]
COIN_MAP = {c["code"]: c for c in COINS}
LIVE_PX = {}
BTC_REGIME = {"state": "NEUTRAL", "allow_alts": True, "label": "⚪ BTC neutrale"}
MACRO_STATUS = {"ok": False}


# ==============================================================================
# 1. UTILITA'
# ==============================================================================
_SECRETS = [s for s in (TELEGRAM_TOKEN, PIONEX_API_KEY, PIONEX_API_SECRET, GITHUB_TOKEN, ANTHROPIC_KEY) if s]


def redact(text):
    t = str(text)
    for s in _SECRETS:
        t = t.replace(s, "***")
    return t


def log(level, msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}][{level}] {redact(msg)}", flush=True)


def _build_session():
    s = requests.Session()
    retry = Retry(total=2, backoff_factor=0.7, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET"]), respect_retry_after_header=True)
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8))
    s.headers.update(HEADERS)
    return s


SESSION = _build_session()
_KRAKEN_LOCK = threading.Lock()
_KRAKEN_LAST = [0.0]
_HIST_LOCK = threading.Lock()


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


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def sigmoid(x):
    return 1.0 / (1.0 + math.exp(-clamp(x, -20.0, 20.0)))


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


def _isnum(s):
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


def summarize(rs):
    """Statistiche di una lista di R-multipli netti (1R = perdita massima pianificata)."""
    n = len(rs)
    if n == 0:
        return {"n": 0, "exp_r": 0.0, "se": 0.0, "wr": 0.0, "pf": 0.0}
    mean = sum(rs) / n
    sd = statistics.pstdev(rs) if n > 1 else 0.0
    gp = sum(r for r in rs if r > 0)
    gl = -sum(r for r in rs if r < 0)
    return {"n": n, "exp_r": mean, "se": sd / math.sqrt(n), "wr": 100.0 * sum(1 for r in rs if r > 0) / n,
            "pf": (gp / gl) if gl > 0 else (9.99 if gp > 0 else 0.0)}


def session_label():
    now = datetime.now(timezone.utc)
    ny = now.astimezone(ZoneInfo("America/New_York"))
    if ny.weekday() < 5:
        o = ny.replace(hour=9, minute=30, second=0, microsecond=0).astimezone(timezone.utc)
        c = ny.replace(hour=16, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        if abs((now - o).total_seconds()) <= 1800:
            return "🔔 Apertura NYSE (alta volatilita')"
        if o < now <= c:
            return "🇺🇸 Wall Street aperta"
    m = now.hour * 60 + now.minute
    if 7 * 60 <= m < 13 * 60:
        return "🇪🇺 Sessione europea"
    return "📅 Weekend (liquidita' ridotta)" if now.weekday() >= 5 else "🌏 Sessione asiatica/notturna"


# ==============================================================================
# 2. STATO PERSISTENTE
# ==============================================================================
def default_state():
    return {
        "position": {
            "is_open": False, "coin_code": "SOL", "symbol": "SOL_USDT", "setup": "", "level": 0.0,
            "entry_price": 0.0, "amount_coin": 0.0, "invested_usdt": 0.0, "max_loss_usdt": 0.0,
            "sl_price": 0.0, "tp_price": 0.0, "initial_sl": 0.0, "one_r": 0.0,
            "highest_price": 0.0, "lowest_price": 0.0, "be_locked": False, "trail_on": False,
            "time_warned": False, "opened_at": "", "opened_ts": 0, "last_check_ts": 0,
            "close_fail_count": 0, "mode": "SIMULATION", "order_id": "", "enter_tag": "",
        },
        "stats": {
            "total_trades": 0, "wins": 0, "losses": 0, "total_pnl_pct": 0.0, "total_pnl_usdt": 0.0,
            "daily_date": "", "daily_pnl_pct": 0.0, "daily_pnl_usdt": 0.0, "daily_trades": 0,
            "daily_risk_usdt": 0.0, "consec_losses": 0, "last_close_ts": 0, "last_loss_ts": 0,
            "coin_last_close": {}, "last_tg_update_id": 0, "last_action": "Nessuna operazione recente",
            "paused_until_ts": 0, "last_report_ts": 0, "funnel": {}, "funnel_yesterday": {},
        },
        "history": [], "pending": None, "reviews": [],
    }


def load_state():
    st = default_state()
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for sec in ("position", "stats"):
                data.setdefault(sec, {})
                for k, v in st[sec].items():
                    data[sec].setdefault(k, v)
            for k in ("history", "pending", "reviews"):
                data.setdefault(k, st[k])
            return data
        except Exception as e:
            log("WARN", f"Impossibile leggere {STATE_FILE}: {e}")
    return st


def save_state(state):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        log("ERROR", f"Salvataggio {STATE_FILE} fallito: {e}")


def roll_daily(stats):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if stats.get("daily_date") != today:
        stats["funnel_yesterday"] = stats.get("funnel", {})
        stats.update({"daily_date": today, "daily_pnl_pct": 0.0, "daily_pnl_usdt": 0.0,
                      "daily_trades": 0, "daily_risk_usdt": 0.0, "consec_losses": 0, "funnel": {}})


# ==============================================================================
# 3. CLIENT PIONEX
# ==============================================================================
def pionex_live_enabled():
    return bool(PIONEX_API_KEY and PIONEX_API_SECRET and AUTO_TRADE_ENABLED)


def pionex_request(method, path, params=None, body=None, timeout=HTTP_TIMEOUT):
    params = {k: v for k, v in (params or {}).items() if v is not None}
    params["timestamp"] = int(time.time() * 1000)
    query = urlencode(sorted(params.items()))
    body_str = json.dumps(body, separators=(",", ":")) if body else ""
    sig = hmac.new(PIONEX_API_SECRET.encode(), f"{method}{path}?{query}{body_str}".encode(), hashlib.sha256).hexdigest()
    headers = {"PIONEX-KEY": PIONEX_API_KEY, "PIONEX-SIGNATURE": sig, "Content-Type": "application/json"}
    try:
        r = SESSION.request(method, f"{PIONEX_BASE}{path}?{query}", data=body_str if body else None,
                            headers=headers, timeout=timeout)
        try:
            rj = r.json()
        except ValueError:
            return {"ok": False, "msg": f"HTTP {r.status_code} risposta non JSON", "data": None}
    except Exception as e:
        return {"ok": False, "msg": f"Errore connessione Pionex: {redact(e)}", "data": None}
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
        return {"min_amount": float(s.get("minAmount") or 10.0),
                "min_qty": float(s.get("minQuantity") or s.get("minTradeSize") or 0.0),
                "base_scale": int(scale), "ok": True}
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
            "clientOrderId": f"ed{int(time.time())}{random.randint(100, 999)}"}
    if side.upper() == "BUY":
        body["amount"] = f"{amount:.2f}"
    else:
        body["size"] = str(size)
    r = pionex_request("POST", "/api/v1/trade/order", body=body)
    if r["ok"]:
        oid = str((r["data"] or {}).get("orderId", "OK"))
        return {"ok": True, "order_id": oid, "msg": f"Ordine Pionex ID {oid}"}
    return {"ok": False, "order_id": "", "msg": f"Rifiuto/errore Pionex: {r['msg']}"}


def live_buy(coin_cfg, usdt, price, live):
    sym, base = coin_cfg["pionex"], coin_cfg["code"]
    if not (live and pionex_live_enabled()):
        entry = price * (1.0 + SLIP_SIDE / 100.0)
        amount = floor_to_scale(usdt / entry * (1 - FEE_SIDE / 100.0), max(coin_cfg["prec"], 2))
        return {"ok": True, "mode": "SIMULATION", "entry_price": entry, "amount_coin": amount,
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
        base_delta, spent, filled = usdt / price * (1 - FEE_SIDE / 100.0), usdt, True
    if not filled:
        return {"ok": False, "mode": "LIVE_ERROR", "msg": res["msg"] if not res["ok"] else "Ordine non risulta eseguito"}
    if spent <= 0:
        spent = usdt
    gross = base_delta / (1 - FEE_SIDE / 100.0)
    entry = spent / gross if gross > 0 else price
    amount = floor_to_scale(base_delta, rules["base_scale"])
    return {"ok": True, "mode": "LIVE_PIONEX", "entry_price": entry, "amount_coin": amount, "invested_usdt": spent,
            "order_id": res["order_id"] or "RECONCILED",
            "msg": f"{res['msg']} | fill reale {amount} {base} per {spent:.2f} USDT"}


def live_sell(pos, coin_cfg, price):
    base, sym = coin_cfg["code"], coin_cfg["pionex"]
    if pos.get("mode") != "LIVE_PIONEX" or not pionex_live_enabled():
        px = price * (1.0 - SLIP_SIDE / 100.0)
        qty = pos["amount_coin"]
        proceeds = qty * px * (1 - FEE_SIDE / 100.0)
        return {"ok": True, "exit_price": px, "sold_qty": qty, "proceeds_usdt": proceeds,
                "pnl_usdt": proceeds - pos["invested_usdt"], "msg": "Vendita simulata"}

    rules = pionex_symbol_rules(sym)
    bal0 = pionex_get_balances()
    if bal0 is None:
        return {"ok": False, "msg": "Impossibile leggere i saldi: vendita non inviata (riprovo al prossimo giro)"}
    free = bal0.get(base, {}).get("free", 0.0)
    size = floor_to_scale(min(pos["amount_coin"], free), rules["base_scale"])
    if size <= 0 or size * price < rules["min_amount"] * 0.98:
        return {"ok": False, "msg": f"Quantita' vendibile {size} {base} sotto il minimo ordine Pionex"}
    res = pionex_place_market_order(sym, "SELL", size=fmt_qty(size, rules["base_scale"]))
    usdt0 = bal0.get("USDT", {}).get("free", 0.0)
    sold, proceeds, bal1 = False, 0.0, None
    for _ in range(2):
        time.sleep(2.0)
        bal1 = pionex_get_balances()
        if bal1 is not None:
            proceeds = bal1.get("USDT", {}).get("free", 0.0) - usdt0
            if bal1.get(base, {}).get("free", 0.0) <= free - 0.5 * size:
                sold = True
                break
    if not sold and res["ok"] and bal1 is None:
        sold, proceeds = True, size * price * (1 - FEE_SIDE / 100.0)
    if not sold:
        return {"ok": False, "msg": res["msg"] if not res["ok"] else "Vendita non risulta eseguita"}
    exit_price = proceeds / (size * (1 - FEE_SIDE / 100.0)) if size > 0 and proceeds > 0 else price
    return {"ok": True, "exit_price": exit_price, "sold_qty": size, "proceeds_usdt": proceeds,
            "pnl_usdt": proceeds - pos["invested_usdt"], "msg": res["msg"]}


# ==============================================================================
# 4. DATI DI MERCATO (storico lungo Binance + cache incrementale compressa)
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


def fetch_kraken_ohlc(pair, interval=60):
    data = safe_get_json("https://api.kraken.com/0/public/OHLC", params={"pair": pair, "interval": interval})
    if not data or data.get("error"):
        return []
    raw = None
    for k, v in data.get("result", {}).items():
        if k != "last" and isinstance(v, list):
            raw = v
            break
    out = []
    for r in raw or []:
        try:
            out.append({"time": int(r[0]), "open": float(r[1]), "high": float(r[2]), "low": float(r[3]),
                        "close": float(r[4]), "volume": float(r[6])})
        except Exception:
            continue
    return out


def fetch_binance_klines(symbol, interval, start_sec=None, limit=1000):
    for host in BINANCE_HOSTS:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_sec:
            params["startTime"] = int(start_sec) * 1000
        j = safe_get_json(f"{host}/api/v3/klines", params=params, retries=1)
        if isinstance(j, list):
            out = []
            for r in j:
                try:
                    out.append({"time": int(r[0]) // 1000, "open": float(r[1]), "high": float(r[2]),
                                "low": float(r[3]), "close": float(r[4]), "volume": float(r[5])})
                except Exception:
                    continue
            return out
    return None


def drop_unfinished(candles, interval_sec):
    if candles and candles[-1]["time"] + interval_sec > time.time():
        return candles[:-1]
    return candles


_HIST = {}
_HIST_DIRTY = [False]


def load_hist_cache():
    if not os.path.exists(HIST_CACHE):
        return
    try:
        with gzip.open(HIST_CACHE, "rt", encoding="utf-8") as f:
            raw = json.load(f)
        with _HIST_LOCK:
            for code, rows in raw.items():
                _HIST[code] = [{"time": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]} for r in rows]
        log("INFO", f"Cache storico caricata: {', '.join(f'{k}:{len(v)}' for k, v in _HIST.items())}")
    except Exception as e:
        log("WARN", f"Cache storico illeggibile ({e}): verra' riscaricato")
        with _HIST_LOCK:
            _HIST.clear()


def save_hist_cache():
    with _HIST_LOCK:
        if not _HIST_DIRTY[0]:
            return
        snap = {code: [[c["time"], c["open"], c["high"], c["low"], c["close"], c["volume"]] for c in rows]
                for code, rows in _HIST.items()}
        _HIST_DIRTY[0] = False
    try:
        tmp = HIST_CACHE + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(snap, f, separators=(",", ":"))
        os.replace(tmp, HIST_CACHE)
    except Exception as e:
        log("WARN", f"Salvataggio cache storico fallito: {e}")


def get_hourly_history(cfg):
    code, now = cfg["code"], time.time()
    with _HIST_LOCK:
        cd = list(_HIST.get(code, []))
    if (not cd) or (now - cd[-1]["time"] > 20 * 86400):
        cd, since = [], int(now - HIST_DAYS * 86400)
    else:
        since = cd[-1]["time"] + 3600
    new = []
    for _ in range(25):
        if since + 3600 > now:
            break
        page = fetch_binance_klines(cfg["binance"], "1h", since, 1000)
        if not page:
            break
        new += page
        since = page[-1]["time"] + 3600
        if len(page) < 1000:
            break
    if new:
        have = {x["time"] for x in cd}
        cd += [x for x in new if x["time"] not in have and x["time"] + 3600 <= now]
        cd.sort(key=lambda x: x["time"])
        cd = cd[-(HIST_DAYS * 24 + 50):]
        with _HIST_LOCK:
            _HIST[code] = cd
            _HIST_DIRTY[0] = True
    if len(cd) < 150:
        k = drop_unfinished(fetch_kraken_ohlc(cfg["kraken"], 60), 3600)
        if len(k) > len(cd):
            log("WARN", f"{code}: storico Binance non disponibile, uso Kraken ({len(k)} barre): edge/ML limitati")
            return k
    return cd


def get_15m(cfg):
    c = fetch_binance_klines(cfg["binance"], "15m", None, 400)
    if not c:
        c = fetch_kraken_ohlc(cfg["kraken"], 15)
    return drop_unfinished(c or [], 900)


def fetch_kraken_orderbook(pair, count=50):
    default = {"imbalance": 0.0, "spread_bps": 5.0, "fetched": False}
    data = safe_get_json("https://api.kraken.com/0/public/Depth", params={"pair": pair, "count": count})
    if not data or data.get("error"):
        return default
    try:
        res = data.get("result", {})
        book = res[list(res.keys())[0]]
        bids, asks = book.get("bids", []), book.get("asks", [])
        bb, ba = float(bids[0][0]), float(asks[0][0])
        mid = (bb + ba) / 2.0
        bw = sum(float(b[0]) * float(b[1]) * math.exp(-max(1e-4, (mid - float(b[0])) / mid) * 100.0) for b in bids)
        aw = sum(float(a[0]) * float(a[1]) * math.exp(-max(1e-4, (float(a[0]) - mid) / mid) * 100.0) for a in asks)
        return {"imbalance": (bw - aw) / (bw + aw) if (bw + aw) > 0 else 0.0,
                "spread_bps": (ba - bb) / mid * 10000.0, "fetched": True}
    except Exception:
        return default


_FUND_CACHE = {"ts": 0.0, "data": {}}


def fetch_all_kraken_futures():
    if time.time() - _FUND_CACHE["ts"] < 1800 and _FUND_CACHE["data"]:
        return _FUND_CACHE["data"]
    out = {}
    for c in COINS:
        j = safe_get_json("https://futures.kraken.com/derivatives/api/v4/historicalfundingrates",
                          params={"symbol": c["futures"]}, retries=1)
        try:
            rates = sorted(j.get("rates") or [], key=lambda r: str(r.get("timestamp", "")))
            rel_hourly = float(rates[-1]["relativeFundingRate"])
            pct8 = rel_hourly * 8.0 * 100.0
            if abs(pct8) <= 0.5:
                out[c["futures"]] = {"funding_8h_pct": pct8, "available": True}
        except Exception:
            continue
    _FUND_CACHE.update(ts=time.time(), data=out)
    if out:
        log("INFO", "Funding 8h: " + ", ".join(f"{k.replace('PF_', '').replace('USD', '')} {v['funding_8h_pct']:+.4f}%" for k, v in out.items()))
    else:
        log("WARN", "Funding non disponibile: filtro funding saltato")
    return out


def fetch_fear_and_greed():
    data = safe_get_json("https://api.alternative.me/fng/?limit=1")
    try:
        item = data["data"][0]
        return int(item.get("value", 50)), item.get("value_classification", "Neutral")
    except Exception:
        return 50, "Neutral"


def fetch_macro_calendar():
    now = datetime.now(timezone.utc)
    events = []
    MACRO_STATUS["ok"] = False
    data = safe_get_json("https://nfs.faireconomy.media/ff_calendar_thisweek.json")
    if isinstance(data, list):
        MACRO_STATUS["ok"] = True
        for it in data:
            try:
                impact = str(it.get("impact", "")).capitalize()
                if str(it.get("country", "")).upper() != "USD" or impact not in ("High", "Medium"):
                    continue
                dt = datetime.fromisoformat(str(it.get("date", ""))).astimezone(timezone.utc)
                diff = (dt - now).total_seconds() / 3600.0
                if -12.0 <= diff <= 36.0:
                    events.append({"title": str(it.get("title", "Evento Macro")), "impact": impact,
                                   "diff_hours": diff, "ts": int(dt.timestamp())})
            except Exception:
                continue
    elif data is None:
        try:
            r = SESSION.get("https://nfs.faireconomy.media/ff_calendar_thisweek.xml", timeout=HTTP_TIMEOUT)
            if r.status_code == 200 and r.text.strip().startswith("<"):
                MACRO_STATUS["ok"] = True
                for ev in ET.fromstring(r.text).findall("event"):
                    impact = (ev.findtext("impact") or "").strip().capitalize()
                    if (ev.findtext("country") or "").strip().upper() != "USD" or impact not in ("High", "Medium"):
                        continue
                    m = re.match(r"(\d{1,2}):(\d{2})(am|pm)", (ev.findtext("time") or "").strip().lower())
                    if not m:
                        continue
                    hh, mm, ap = int(m.group(1)), int(m.group(2)), m.group(3)
                    hh = hh + 12 if (ap == "pm" and hh < 12) else (0 if (ap == "am" and hh == 12) else hh)
                    dt = datetime.strptime(f"{(ev.findtext('date') or '').strip()} {hh:02d}:{mm:02d}",
                                           "%m-%d-%Y %H:%M").replace(tzinfo=timezone.utc)
                    diff = (dt - now).total_seconds() / 3600.0
                    if -12.0 <= diff <= 36.0:
                        events.append({"title": (ev.findtext("title") or "Evento USD").strip(), "impact": impact,
                                       "diff_hours": diff, "ts": int(dt.timestamp())})
        except Exception:
            pass
    events.sort(key=lambda x: x["ts"])
    return events


# ==============================================================================
# 5. INDICATORI QUANTITATIVI
# ==============================================================================
def calc_ema_series(values, period):
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def calc_rsi_series(closes, period=14):
    if len(closes) < period + 1:
        return [50.0] * len(closes)
    rsis = [50.0] * period
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, period + 1)]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, period + 1)]
    ag, al = sum(gains) / period, sum(losses) / period
    rsis.append(100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al))
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(0.0, d)) / period
        al = (al * (period - 1) + max(0.0, -d)) / period
        rsis.append(100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al))
    return rsis


def calc_atr_series(candles, period=14):
    if len(candles) < 2:
        return [0.0] * len(candles)
    trs = [candles[0]["high"] - candles[0]["low"]]
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return calc_ema_series(trs, period)


def calc_adx_series(candles, period=14):
    n = len(candles)
    if n < period * 2 + 2:
        return [20.0] * n
    tr, pdm, mdm = [0.0], [0.0], [0.0]
    for i in range(1, n):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        up, dn = h - candles[i - 1]["high"], candles[i - 1]["low"] - l
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
        pdm.append(up if (up > dn and up > 0) else 0.0)
        mdm.append(dn if (dn > up and dn > 0) else 0.0)
    a, p, m = calc_ema_series(tr, period), calc_ema_series(pdm, period), calc_ema_series(mdm, period)
    dx = []
    for av, pv, mv in zip(a, p, m):
        if av <= 0:
            dx.append(0.0)
            continue
        pdi, mdi = 100.0 * pv / av, 100.0 * mv / av
        dx.append(100.0 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0.0)
    return calc_ema_series(dx, period)


def calc_bbw_series(closes, period=20):
    n = len(closes)
    out = [0.0] * n
    for k in range(period - 1, n):
        w = closes[k - period + 1:k + 1]
        m = sum(w) / period
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / period)
        out[k] = 4.0 * sd / m if m > 0 else 0.0
    for k in range(min(period - 1, n)):
        out[k] = out[period - 1] if n >= period else 0.0
    return out


def calc_vwap_series(candles, window=24):
    out = []
    for i in range(len(candles)):
        lo = max(0, i - window + 1)
        pv, vv = 0.0, 0.0
        for k in range(lo, i + 1):
            tp = (candles[k]["high"] + candles[k]["low"] + candles[k]["close"]) / 3.0
            v = max(candles[k]["volume"], 0.0)
            pv += tp * v
            vv += v
        out.append(pv / vv if vv > 0 else candles[i]["close"])
    return out


def calc_efficiency_ratio(closes, period=10):
    """Kaufman Efficiency Ratio (0 = rumore/chop, 1 = trend direzionale pulito)."""
    out = [0.3] * len(closes)
    for i in range(period, len(closes)):
        change = abs(closes[i] - closes[i - period])
        vol = sum(abs(closes[k] - closes[k - 1]) for k in range(i - period + 1, i + 1))
        out[i] = change / vol if vol > 0 else 0.0
    return out


def prep_series(candles):
    c = [x["close"] for x in candles]
    t = [x["time"] for x in candles]
    return {"t": t, "idx": {tt: k for k, tt in enumerate(t)}, "c": c, "h": [x["high"] for x in candles],
            "l": [x["low"] for x in candles], "v": [x["volume"] for x in candles],
            "ema20": calc_ema_series(c, 20), "ema50": calc_ema_series(c, 50), "ema100": calc_ema_series(c, 100),
            "atr": calc_atr_series(candles, 14), "rsi": calc_rsi_series(c, 14), "adx": calc_adx_series(candles, 14),
            "bbw": calc_bbw_series(c, 20), "vwap24": calc_vwap_series(candles, 24), "er": calc_efficiency_ratio(c, 10)}


# ==============================================================================
# 6. SETUP (5 pattern coerenti tra live e backtest) + 23 FEATURE ML
# ==============================================================================
SETUPS = ("breakout_24h", "squeeze_break", "flag_break", "dip_bounce", "reclaim_ma20")


def detect_setup(S, i, bt, price=None, vr=1.0, fresh=None):
    c, h, l = S["c"], S["h"], S["l"]
    if i < 130 or (bt and i >= len(c)):
        return []
    j = i if bt else i - 1
    px = c[i] if bt else price
    atr = S["atr"][j]
    if not px or atr <= 0:
        return []
    e20, e50, e100 = S["ema20"][j], S["ema50"][j], S["ema100"][j]
    if not (e20 > e50 and px > e100):
        return []
    if px / c[i - 25] - 1.0 > MAX_PUMP_24H_PCT / 100.0:
        return []
    rsi, adx = S["rsi"][j], S["adx"][j]

    def crossed(level):
        return (c[i - 1] <= level) if bt else bool(fresh and fresh(level))

    def ext_ok(level, k=MAX_EXT_ATR):
        return (px - level) / atr <= k

    out = []
    lvl = max(h[i - 24:i])
    if px > lvl and crossed(lvl) and adx >= SETUP_ADX_MIN and 50 <= rsi <= 75 and vr >= SETUP_VR_MIN and ext_ok(lvl):
        out.append(("breakout_24h", lvl))

    bbw = S["bbw"]
    ref = sorted(bbw[i - 120:i])
    if min(bbw[i - 6:i]) <= ref[len(ref) // 5]:
        lv2 = max(h[i - 12:i])
        if px > lv2 and crossed(lv2) and 48 <= rsi <= 78 and vr >= SETUP_VR_MIN and ext_ok(lv2):
            out.append(("squeeze_break", lv2))

    pole = c[i - 7] / c[i - 17] - 1.0
    f_hi, f_lo = max(h[i - 6:i]), min(l[i - 6:i])
    if (pole >= 0.03 and (f_hi - f_lo) / c[i - 1] <= 0.03 and px > f_hi and crossed(f_hi)
            and vr >= SETUP_VR_MIN and rsi <= 75 and ext_ok(f_hi)):
        out.append(("flag_break", f_hi))

    if min(S["rsi"][i - 6:i]) <= 38:
        lv3 = max(h[i - 3:i])
        if px > lv3 and crossed(lv3) and 40 <= rsi <= 62 and vr >= 0.8 * SETUP_VR_MIN and ext_ok(lv3, MAX_EXT_ATR + 0.2):
            out.append(("dip_bounce", lv3))

    pulled = any(c[k] < S["ema20"][k] for k in range(i - 4, i))
    if pulled and px > e20 and crossed(e20) and 40 <= rsi <= 65 and vr >= SETUP_VR_MIN and (px - e20) / atr <= 1.0:
        out.append(("reclaim_ma20", e20))
    return out


def btc_context(btc, t):
    if not btc:
        return 0.0, 0.5, 0.0
    k = btc["idx"].get(t)
    if k is None or k < 30:
        return 0.0, 0.5, 0.0
    return (btc["c"][k] / btc["c"][k - 24] - 1.0, 1.0 if btc["c"][k] > btc["ema50"][k] else 0.0,
            btc["atr"][k] / btc["c"][k] * 100.0)


def make_features(S, i, bt, px, vr, setup, level, btc=None):
    """23 feature: momentum, volatilita', VWAP Z-score, Kaufman ER, BB rank, stagionalita', BTC, RS, setup one-hot."""
    j = i if bt else i - 1
    atr = max(S["atr"][j], 1e-12)
    t = S["t"][j]
    ang = 2.0 * math.pi * ((t // 3600) % 24) / 24.0
    win = S["bbw"][max(0, j - 120):j + 1]
    bb_rank = sum(1 for x in win if x <= S["bbw"][j]) / max(1, len(win))
    atr_ratio = clamp(atr / max(sum(S["atr"][j - 120:j]) / 120.0, 1e-12), 0.0, 3.0) / 3.0
    ret24 = px / S["c"][i - 25] - 1.0
    b_ret, b_above, b_vol = btc_context(btc, t)
    vwap_z = clamp((px - S["vwap24"][j]) / atr, -5.0, 5.0) / 5.0
    f = [S["rsi"][j] / 100.0 - 0.5, S["adx"][j] / 50.0, min(vr, 4.0) / 4.0, (px - level) / atr,
         atr / px * 100.0, clamp(ret24, -0.3, 0.3) * 10.0,
         clamp((S["ema20"][j] - S["ema50"][j]) / atr, -5.0, 5.0) / 5.0,
         clamp(px / S["c"][i - 120] - 1.0, -0.4, 0.4) * 5.0, bb_rank, atr_ratio,
         vwap_z, S["er"][j],
         math.sin(ang), math.cos(ang),
         clamp(b_ret, -0.15, 0.15) * 10.0, b_above, b_vol,
         clamp(ret24 - b_ret, -0.2, 0.2) * 10.0]
    f += [1.0 if setup == s else 0.0 for s in SETUPS]
    return f


# ==============================================================================
# 7. PIANO DI USCITA E SIZE
# ==============================================================================
def build_plan(entry, atr, swing_low, target_r=None):
    tr = TARGET_R if target_r is None else target_r
    if entry <= 0 or atr <= 0:
        return {"ok": False, "reason": "dati insufficienti per il piano"}
    stop_raw = min(entry - ATR_STOP_K * atr, swing_low - 0.1 * atr)
    risk = max(entry - stop_raw, entry * MIN_STOP_PCT / 100.0)
    risk_pct = risk / entry * 100.0
    plan = {"ok": True, "reason": "", "entry": entry, "stop": entry - risk, "target": entry + tr * risk,
            "risk": risk, "one_r": risk, "risk_pct": risk_pct, "target_r": tr,
            "rr_net": (tr * risk_pct - COST_RT) / (risk_pct + COST_RT)}
    if risk_pct > MAX_STOP_PCT:
        plan.update(ok=False, reason=f"stop troppo lontano ({risk_pct:.2f}% > {MAX_STOP_PCT:.1f}%)")
    elif plan["rr_net"] < MIN_RR_NET:
        plan.update(ok=False, reason=f"R:R netto {plan['rr_net']:.2f} < {MIN_RR_NET} (stop troppo stretto per i costi)")
    return plan


def manage_stop(entry, stop, one_r, highest):
    r_hi = (highest - entry) / one_r if one_r > 0 else 0.0
    new = stop
    if r_hi >= BE_AT_R:
        new = max(new, entry * (1.0 + COST_RT / 100.0))
    if r_hi >= TRAIL_START_R:
        new = max(new, highest - TRAIL_R * one_r)
    return new


def net_r(entry, exit_px, risk_pct):
    s = SLIP_SIDE / 100.0
    pnl = ((exit_px * (1 - s)) / (entry * (1 + s)) - 1.0) * 100.0 - 2.0 * FEE_SIDE
    return pnl / (risk_pct + COST_RT)


def size_position(capital_usdt, plan, risk_mult=1.0):
    max_loss = capital_usdt * RISK_PER_TRADE_PCT * risk_mult / 100.0
    pos = max_loss / ((plan["risk_pct"] + COST_RT) / 100.0)
    usable = capital_usdt * (1.0 - CASH_BUFFER_PCT / 100.0)
    limit = "perdita massima"
    if pos > usable:
        pos, limit = usable, "cassa disponibile"
    return pos, pos * (plan["risk_pct"] + COST_RT) / 100.0, limit


# ==============================================================================
# 8. TIME MACHINE: backtest + Monte Carlo + ML Ensemble (LogReg + HistGB + MLP)
# ==============================================================================
def simulate_trade(S, i, plan, H):
    c, h, l = S["c"], S["h"], S["l"]
    entry, one_r, stop, tgt = plan["entry"], plan["one_r"], plan["stop"], plan["target"]
    highest, exit_px = entry, None
    for k in range(1, H + 1):
        j = i + k
        if l[j] <= stop:
            exit_px = stop - 0.25 * max(0.0, stop - l[j])
            break
        if h[j] >= tgt:
            exit_px = tgt
            break
        highest = max(highest, h[j])
        stop = manage_stop(entry, stop, one_r, highest)
    if exit_px is None:
        exit_px = c[i + H]
    return net_r(entry, exit_px, plan["risk_pct"])


def backtest_coin(code, S, target_r=None, btc=None):
    n, H = len(S["c"]), MAX_HOLD_H
    events, last = [], -10 ** 9
    for i in range(130, n - H - 1):
        if i - last < H:
            continue
        base = sum(S["v"][i - 24:i]) / 24.0
        vr = S["v"][i] / base if base > 0 else 1.0
        cands = detect_setup(S, i, True, vr=vr)
        if not cands:
            continue
        setup, level = cands[0]
        plan = build_plan(S["c"][i], S["atr"][i], min(S["l"][i - 2:i + 1]), target_r)
        if not plan["ok"]:
            continue
        events.append({"code": code, "setup": setup, "t": S["t"][i], "R": simulate_trade(S, i, plan, H),
                       "feat": make_features(S, i, True, S["c"][i], vr, setup, level, btc)})
        last = i
    return events


def _models():
    return [make_pipeline(StandardScaler(), LogisticRegression(C=0.3, max_iter=500)),
            HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=120, l2_regularization=1.0,
                                           min_samples_leaf=20, random_state=7),
            make_pipeline(StandardScaler(), MLPClassifier(hidden_layer_sizes=(16, 8), alpha=0.01, max_iter=400,
                                                          early_stopping=True, validation_fraction=0.15, random_state=7))]


def _fit_ens(X, y):
    if len(set(y.tolist())) < 2:
        return []
    ms = []
    for m in _models():
        try:
            m.fit(X, y)
            ms.append(m)
        except Exception as e:
            log("WARN", f"Modello ML non addestrato: {e}")
    return ms


def _predict_ens(ms, X):
    return np.mean([m.predict_proba(X)[:, 1] for m in ms], axis=0)


def _score(ml, feat):
    return float(_predict_ens(ml["models"], np.array([feat], dtype=float))[0])


def train_edge_model(events):
    res = {"active": False, "why": "", "n": len(events)}
    if not ML_ENABLED:
        res["why"] = "disattivato (ML_ENABLED=false)"
        return res
    if not ML_OK:
        res["why"] = "scikit-learn/numpy non installati"
        return res
    ev = sorted(events, key=lambda e: e["t"])
    n = len(ev)
    if n < ML_MIN_EVENTS:
        res["why"] = f"eventi insufficienti ({n} < {ML_MIN_EVENTS})"
        return res
    X = np.array([e["feat"] for e in ev], dtype=float)
    R = np.array([e["R"] for e in ev], dtype=float)
    T = np.array([e["t"] for e in ev], dtype=float)
    y = (R > 0).astype(int)
    bounds = [int(n * 0.4), int(n * 0.6), int(n * 0.8), n]
    oos_s, oos_r, folds_ok = [], [], 0
    for f in range(3):
        a, b = bounds[f], bounds[f + 1]
        tr = np.where(T[:a] < T[a] - MAX_HOLD_H * 3600)[0]
        if len(tr) < 60 or b - a < 15:
            continue
        ms = _fit_ens(X[tr], y[tr])
        if not ms:
            continue
        sc = _predict_ens(ms, X[a:b])
        if len(set(y[a:b].tolist())) == 2 and roc_auc_score(y[a:b], sc) > 0.5:
            folds_ok += 1
        oos_s += sc.tolist()
        oos_r += R[a:b].tolist()
    if len(oos_s) < 60:
        res["why"] = "campione fuori campione insufficiente"
        return res
    oos_s, oos_r = np.array(oos_s), np.array(oos_r)
    auc = float(roc_auc_score(oos_r > 0, oos_s)) if len(set((oos_r > 0).tolist())) == 2 else 0.5
    thr = float(np.quantile(oos_s, ML_SKIP_QUANTILE))
    kept = oos_r[oos_s >= thr]
    all_r = float(oos_r.mean())
    kept_r = float(kept.mean()) if len(kept) else all_r
    res.update(auc=auc, kept_r=kept_r, all_r=all_r, n_oos=len(oos_r), folds_ok=folds_ok, thr=thr)
    if auc >= ML_MIN_AUC and kept_r >= all_r + 0.05 and folds_ok >= 2:
        ms = _fit_ens(X, y)
        if ms:
            res.update(active=True, models=ms)
            return res
    res["why"] = f"non validato fuori campione (AUC {auc:.2f}, tenuti {kept_r:+.2f}R vs tutti {all_r:+.2f}R, {folds_ok}/3 blocchi)"
    return res


def monte_carlo_plan(c15, plan, paths=None, seed=19):
    """Bootstrap a blocchi dei rendimenti 15m (senza drift) con gestione realistica di BE e trailing."""
    paths = paths or MC_PATHS
    closes = [x["close"] for x in c15[-600:]]
    rets = [math.log(closes[k] / closes[k - 1]) for k in range(1, len(closes)) if closes[k - 1] > 0]
    if len(rets) < 100:
        return None
    mu = sum(rets) / len(rets)
    rets = [r - mu for r in rets]
    steps, block, rng = MAX_HOLD_H * 4, 4, random.Random(seed)
    entry, one_r, tgt = plan["entry"], plan["one_r"], plan["target"]
    ht = hs = 0
    rsum = 0.0
    for _ in range(paths):
        p, hi, stop, k, res = entry, entry, plan["stop"], 0, None
        while k < steps and res is None:
            st = rng.randrange(0, len(rets) - block)
            for b in range(block):
                p *= math.exp(rets[st + b])
                k += 1
                if p <= stop:
                    res = ("s", stop)
                    break
                if p >= tgt:
                    res = ("t", tgt)
                    break
                if p > hi:
                    hi = p
                    stop = manage_stop(entry, stop, one_r, hi)
                if k >= steps:
                    break
        if res is None:
            res = ("n", p)
        ht += res[0] == "t"
        hs += res[0] == "s"
        rsum += net_r(entry, res[1], plan["risk_pct"])
    return {"p_target": ht / paths, "p_stop": hs / paths, "p_open": 1.0 - (ht + hs) / paths,
            "avg_r": rsum / paths, "seed": seed, "paths": paths}


_EDGE = {"key": None, "codes": 0, "n": 0, "by_setup": {}, "by_coin": {}, "recent_by_setup": {}, "all": summarize([]),
         "days": 0, "ml": {"active": False, "why": "n/d"}}


def refresh_edge(series_by_code):
    global _EDGE
    key = max(S["t"][-1] for S in series_by_code.values())
    if _EDGE["key"] == key and _EDGE["codes"] == len(series_by_code):
        return _EDGE
    btc = series_by_code.get("BTC")
    events = []
    for code, S in series_by_code.items():
        events += backtest_coin(code, S, btc=btc)
    by_setup, by_coin, rec = {}, {}, {}
    cut = time.time() - RECENT_DAYS * 86400
    for e in events:
        by_setup.setdefault(e["setup"], []).append(e["R"])
        by_coin.setdefault((e["code"], e["setup"]), []).append(e["R"])
        if e["t"] >= cut:
            rec.setdefault(e["setup"], []).append(e["R"])
    days = int(min(len(S["c"]) for S in series_by_code.values()) / 24)
    _EDGE = {"key": key, "codes": len(series_by_code), "n": len(events), "days": days,
             "by_setup": {k: summarize(v) for k, v in by_setup.items()},
             "by_coin": {k: summarize(v) for k, v in by_coin.items()},
             "recent_by_setup": {k: summarize(v) for k, v in rec.items()},
             "all": summarize([e["R"] for e in events]), "ml": train_edge_model(events)}
    log("INFO", f"Time Machine aggiornata: {len(events)} eventi su ~{days}g, expectancy {_EDGE['all']['exp_r']:+.2f}R, "
                f"ML {'ATTIVO' if _EDGE['ml'].get('active') else 'spento (' + str(_EDGE['ml'].get('why', ''))[:60] + ')'}")
    return _EDGE


# ==============================================================================
# 9. VALUTAZIONE COIN + GATE DI RISCHIO
# ==============================================================================
def blank_eval(cfg, price, **kw):
    ev = {"coin": cfg, "code": cfg["code"], "price": price, "ref_close_15m": price, "setup": None, "level": 0.0,
          "plan": {"ok": False, "reason": "n/d"}, "feat": None, "atr": 0.0, "rsi_1h": 50.0, "rsi_15m": 50.0,
          "adx": 20.0, "vr": 1.0, "er": 0.3, "vwap_z": 0.0, "ret24": 0.0, "ret_1h": 0.0, "ret_4h": 0.0,
          "ema50_1h": price, "trend_ok": False, "near_level": 0.0, "dist_atr": 0.0,
          "ob": {"imbalance": 0.0, "spread_bps": 5.0, "fetched": False},
          "fut": {"funding_8h_pct": 0.0, "available": False}, "c15_recent": [], "score": 0, "tm": None, "ml_p": None,
          "verdict": {"ok": False, "blocks": ["dati non disponibili"]}, "fallback": True}
    ev.update(kw)
    return ev


def evaluate_coin(coin_cfg, futures_map):
    code, pair = coin_cfg["code"], coin_cfg["kraken"]
    c1h = get_hourly_history(coin_cfg)
    c15 = get_15m(coin_cfg)
    if len(c1h) < 150 or len(c15) < 100:
        return None
    S, n = prep_series(c1h), len(c1h)
    cl15 = [x["close"] for x in c15]
    ref = cl15[-1]
    px = LIVE_PX.get(coin_cfg["pionex"])
    price = px if (px and abs(px / ref - 1.0) < 0.03) else ref
    v15 = [x["volume"] for x in c15]
    base = sum(v15[-100:-4]) / 24.0
    vr = sum(v15[-4:]) / base if base > 0 else 1.0

    # Finestra 3 barre 15m (45 min): protegge da ritardi di cron/rescan senza inseguire (filtrato da MAX_EXT_ATR)
    def fresh(lv):
        return cl15[-1] > lv and any(x <= lv for x in cl15[-4:-1])

    cands = detect_setup(S, n, False, price=price, vr=vr, fresh=fresh)
    setup, level = cands[0] if cands else (None, 0.0)
    atr = S["atr"][n - 1]
    plan = build_plan(price, atr, min(S["l"][n - 3:n]))
    near = max(S["h"][n - 24:n])
    ev = blank_eval(coin_cfg, price, ref_close_15m=ref, setup=setup, level=level, plan=plan, atr=atr,
                    rsi_1h=S["rsi"][n - 1], rsi_15m=calc_rsi_series(cl15, 14)[-1], adx=S["adx"][n - 1], vr=vr,
                    er=S["er"][n - 1], vwap_z=(price - S["vwap24"][n - 1]) / atr if atr > 0 else 0.0,
                    ret24=price / S["c"][n - 25] - 1.0, ret_1h=price / cl15[-5] - 1.0, ret_4h=price / cl15[-17] - 1.0,
                    ema50_1h=S["ema50"][n - 1], near_level=near, dist_atr=(near - price) / atr if atr > 0 else 0.0,
                    trend_ok=bool(S["ema20"][n - 1] > S["ema50"][n - 1] and price > S["ema100"][n - 1]),
                    fut=futures_map.get(coin_cfg["futures"], {"funding_8h_pct": 0.0, "available": False}),
                    c15_recent=[{"time": x["time"], "high": x["high"], "low": x["low"]} for x in c15[-48:]],
                    fallback=False)
    ev["_S"], ev["_c15"], ev["_n"] = S, c15, n
    if setup:
        ev["ob"] = fetch_kraken_orderbook(pair)
    return ev


def update_btc_regime(ev):
    dump = (ev["rsi_15m"] < 32 and ev["ret_1h"] < -0.011) or ev["ret_4h"] < -0.03
    weak = ev["price"] < ev["ema50_1h"] and ev["ret24"] < -0.02
    BTC_REGIME.update({"state": "DUMP" if dump else ("WEAK" if weak else "OK"), "allow_alts": not (dump or weak),
                       "label": "🚨 BTC in dump: altcoin bloccate" if dump else
                                ("🟠 BTC debole: altcoin bloccate" if weak else "🟢 BTC stabile")})


def scan_all_coins(coins, futures_map):
    results = {}
    btc = COIN_MAP["BTC"]
    if any(c["code"] == "BTC" for c in coins):
        try:
            r = evaluate_coin(btc, futures_map)
            if r:
                results["BTC"] = r
                update_btc_regime(r)
        except Exception as e:
            log("WARN", f"Errore analisi BTC: {e}")
    rest = [c for c in coins if c["code"] not in results]
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut = {ex.submit(evaluate_coin, cfg, futures_map): cfg["code"] for cfg in rest}
        for f in as_completed(fut):
            try:
                r = f.result()
                if r:
                    results[fut[f]] = r
            except Exception as e:
                log("WARN", f"Errore analisi {fut[f]}: {e}")
    save_hist_cache()
    return results


def compute_score(ev, edge):
    trend = (10.0 if ev["trend_ok"] else 0.0) + min(ev["adx"], 40.0) / 40.0 * 12.0 + clamp(ev.get("er", 0.3), 0.0, 0.7) / 0.7 * 5.0
    rs = clamp(ev.get("rs_vs_btc", 0.0) / 0.03, -1.0, 1.0) * 10.0 + 10.0
    mom = rs + (5.0 if 45 <= ev["rsi_1h"] <= 68 else 0.0)
    vol = min(ev["vr"], 3.0) / 3.0 * 14.0
    ob = ev["ob"]
    micro = clamp((ob["imbalance"] + 0.2) / 0.4, 0.0, 1.0) * 7.0 + (3.0 if ob["spread_bps"] <= 10 else 0.0)
    st = edge["by_setup"].get(ev["setup"] or "", None)
    e = clamp(st["exp_r"] / 0.4, 0.0, 1.0) * 24.0 if st and st["n"] >= MIN_SETUP_N else 0.0
    return int(round(trend + mom + vol + micro + e))


def strategy_proof(state):
    hs = [h for h in state["history"] if h.get("ver") == STRAT_VER and "r_mult" in h][:40]
    s = summarize([h["r_mult"] for h in hs])
    s["ok"] = s["n"] >= PROOF_MIN_TRADES and s["exp_r"] >= PROOF_MIN_EXP_R and s["pf"] >= 1.15
    return s


def trading_mode(state):
    if not pionex_live_enabled():
        return "SIMULATION", "live non abilitato"
    if LIVE_PROOF_REQUIRED:
        pr = strategy_proof(state)
        if not pr["ok"]:
            return "SIMULATION", f"prova in corso: {pr['n']}/{PROOF_MIN_TRADES} trade, {pr['exp_r']:+.2f}R, PF {pr['pf']:.2f}"
    lv = [h["r_mult"] for h in state["history"] if h.get("ver") == STRAT_VER and h.get("mode") == "LIVE_PIONEX"
          and "r_mult" in h][:10]
    if len(lv) >= 8 and sum(lv) / len(lv) < -0.25:
        return "SIMULATION", "demozione: ultimi trade live negativi"
    return "LIVE_PIONEX", "prova superata"


def evaluate_gates(ev, edge, state, capital_usdt, fng_val, high_soon):
    b = []
    code, setup, plan, now = ev["code"], ev["setup"], ev["plan"], datetime.now(timezone.utc)
    usable_usdt = capital_usdt * (1.0 - CASH_BUFFER_PCT / 100.0)
    out = {"ok": False, "blocks": b, "size_usdt": 0.0, "usable_usdt": usable_usdt,
           "max_loss_usdt": 0.0, "limit": "", "risk_mult": 1.0}
    if not setup:
        b.append("nessun setup attivo")
        return out
    stats, now_ts = state["stats"], int(now.timestamp())

    # --- edge (Time Machine): pool + shrinkage bayesiano + regime recente ---
    pool, st = edge["all"], edge["by_setup"].get(setup)
    if pool["n"] < MIN_POOL_N:
        b.append(f"edge non verificabile (pool {pool['n']} casi < {MIN_POOL_N}, storico ~{edge.get('days', 0)}g)")
    elif not st or st["n"] < MIN_SETUP_N:
        b.append(f"edge non verificabile ({setup}: {st['n'] if st else 0} casi < {MIN_SETUP_N})")
    else:
        shrunk = (st["n"] * st["exp_r"] + SHRINK_K * pool["exp_r"]) / (st["n"] + SHRINK_K)
        if shrunk < MIN_EXP_R:
            b.append(f"edge storico insufficiente ({setup}: {st['exp_r']:+.2f}R su {st['n']} casi, stima {shrunk:+.2f}R)")
        rc = edge["recent_by_setup"].get(setup)
        if rc and rc["n"] >= RECENT_MIN_N and rc["exp_r"] < RECENT_MIN_EXP_R:
            b.append(f"regime recente sfavorevole ({setup}: {rc['exp_r']:+.2f}R su {rc['n']} casi negli ultimi {RECENT_DAYS}g)")

    own = edge["by_coin"].get((code, setup))
    if own and own["n"] >= 8 and own["exp_r"] < -0.30:
        b.append(f"{code} storicamente negativa su {setup} ({own['exp_r']:+.2f}R su {own['n']})")
    ml = edge["ml"]
    if ml.get("active") and ev.get("feat"):
        p = _score(ml, ev["feat"])
        ev["ml_p"] = p
        if p < ml["thr"]:
            b.append(f"filtro ML sfavorevole ({p:.2f} < {ml['thr']:.2f})")
        elif RISK_SCALE_BY_ML:
            out["risk_mult"] = clamp(0.8 + (p - ml["thr"]) * 2.0, 0.8, 1.2)

    # --- piano + simulazione Monte Carlo ---
    if not plan["ok"]:
        b.append(plan["reason"])
    else:
        tm = monte_carlo_plan(ev["_c15"], plan)
        ev["tm"] = tm
        if tm and tm["p_target"] < MC_MIN_P_TARGET:
            b.append(f"target poco raggiungibile in {MAX_HOLD_H}h (MC {tm['p_target'] * 100:.0f}% < {MC_MIN_P_TARGET * 100:.0f}%)")

    # --- contesto ---
    if code != "BTC" and not BTC_REGIME["allow_alts"]:
        b.append("BTC debole/in dump")
    if high_soon:
        b.append("evento macro USD High-impact (-30m/+2h)")
    if ev["fut"].get("available") and ev["fut"]["funding_8h_pct"] > FUNDING_MAX_PCT:
        b.append(f"funding troppo alto ({ev['fut']['funding_8h_pct']:+.3f}%): mercato affollato")
    if ev["ob"].get("fetched") and ev["ob"]["spread_bps"] > MAX_SPREAD_BPS:
        b.append(f"spread alto ({ev['ob']['spread_bps']:.1f} bps)")
    if ev["ref_close_15m"] > 0 and ev["price"] / ev["ref_close_15m"] - 1.0 > 0.012:
        b.append("spike improvviso sopra la chiusura 15m")
    if ev["score"] < ENTRY_SCORE_MIN:
        b.append(f"punteggio {ev['score']} < {ENTRY_SCORE_MIN}")

    # --- limiti ---
    if not (ENTRY_START_UTC <= now.hour < ENTRY_END_UTC):
        b.append(f"fuori finestra {ENTRY_START_UTC:02d}-{ENTRY_END_UTC:02d} UTC")
    if stats.get("paused_until_ts", 0) > now_ts:
        b.append("pausa per edge in calo (ultimi trade negativi)")
    if stats["daily_trades"] >= MAX_TRADES_PER_DAY:
        b.append(f"max {MAX_TRADES_PER_DAY} trade/giorno")
    if stats["consec_losses"] >= MAX_CONSEC_LOSSES:
        b.append(f"{MAX_CONSEC_LOSSES} stop consecutivi")
    if stats["daily_pnl_pct"] <= -DAILY_MAX_LOSS_PCT:
        b.append(f"circuit breaker giornaliero ({stats['daily_pnl_pct']:+.2f}%)")
    if stats["last_close_ts"] and now_ts - stats["last_close_ts"] < MIN_MINUTES_BETWEEN_TRADES * 60:
        b.append("cooldown dopo ultima chiusura")
    if stats["last_loss_ts"] and now_ts - stats["last_loss_ts"] < COOLDOWN_AFTER_LOSS_MIN * 60:
        b.append("cooldown dopo perdita")
    cl = stats.get("coin_last_close", {}).get(code, 0)
    if cl and now_ts - cl < SAME_COIN_COOLDOWN_H * 3600:
        b.append(f"{code} chiusa da meno di {SAME_COIN_COOLDOWN_H:.0f}h")

    # --- size ---
    if plan["ok"]:
        pos, max_loss, limit = size_position(capital_usdt, plan, out["risk_mult"])
        out.update(size_usdt=pos, max_loss_usdt=max_loss, limit=limit)
        if pos < MIN_ORDER_USDT:
            b.append(f"ordine {pos:.2f} USDT sotto il minimo {MIN_ORDER_USDT}")
        if stats["daily_risk_usdt"] + max_loss > capital_usdt * DAILY_MAX_LOSS_PCT / 100.0:
            b.append("rischio giornaliero cumulato oltre il limite")
    out["ok"] = not b
    return out


def _funnel(stats, ev):
    fun = stats.setdefault("funnel", {})

    def bump(k):
        fun[k] = fun.get(k, 0) + 1

    bump("valutazioni")
    if not ev["setup"]:
        bump("nessun_setup")
        return
    bump("setup_attivo")
    for blk in ev["verdict"]["blocks"]:
        bump("BLOCCO: " + re.sub(r"[-+]?\d+(\.\d+)?%?", "#", blk)[:45])
    if ev["verdict"]["ok"]:
        bump("passati")


def finalize_signals(all_evals, state, eur_usd, macro_events, fng_val):
    series = {c: ev["_S"] for c, ev in all_evals.items() if "_S" in ev}
    edge = refresh_edge(series) if series else _EDGE
    capital = INVEST_EUR * eur_usd
    roll_daily(state["stats"])
    high_soon = any(e["impact"] == "High" and -0.5 <= e["diff_hours"] <= 2.0 for e in macro_events)
    btc_ret = all_evals["BTC"]["ret24"] if "BTC" in all_evals else 0.0
    btc_S = all_evals["BTC"].get("_S") if "BTC" in all_evals else None
    for code, ev in all_evals.items():
        if ev.get("fallback"):
            continue
        ev["rs_vs_btc"] = 0.0 if code == "BTC" else ev["ret24"] - btc_ret
        ev["score"] = compute_score(ev, edge)
        if ev["setup"]:
            ev["feat"] = make_features(ev["_S"], ev["_n"], False, ev["price"], ev["vr"], ev["setup"], ev["level"], btc_S)
        ev["verdict"] = evaluate_gates(ev, edge, state, capital, fng_val, high_soon)
        _funnel(state["stats"], ev)
    ranked = sorted(all_evals.values(), key=lambda e: (e["verdict"]["ok"], bool(e["setup"]), e["score"]), reverse=True)
    return ranked, edge, high_soon


# ==============================================================================
# 10. REVISORE CLAUDE (opzionale: veto + risk_mult) + APPROVAZIONE
# ==============================================================================
def claude_review(ev, edge, macro_events, fng_val):
    if not (CLAUDE_REVIEW and ANTHROPIC_KEY):
        return {"veto": False, "risk_mult": 1.0, "reason": "revisore disattivato"}
    p = ev["plan"]
    facts = {"coin": ev["code"], "setup": ev["setup"], "prezzo": ev["price"], "livello": ev["level"],
             "stop": p["stop"], "target": p["target"], "rischio_pct": round(p["risk_pct"], 2),
             "rr_netto": round(p["rr_net"], 2), "rsi_1h": round(ev["rsi_1h"], 1), "adx_1h": round(ev["adx"], 1),
             "efficiency_ratio": round(ev.get("er", 0.3), 2), "vwap_z_atr": round(ev.get("vwap_z", 0.0), 2),
             "ritorno_24h_pct": round(ev["ret24"] * 100, 2), "forza_vs_btc_pct": round(ev.get("rs_vs_btc", 0) * 100, 2),
             "funding_8h_pct": ev["fut"]["funding_8h_pct"], "fear_greed": fng_val, "regime_btc": BTC_REGIME["state"],
             "ml_probabilita": None if ev.get("ml_p") is None else round(ev["ml_p"], 3),
             "storico_setup": {k: round(v, 3) if isinstance(v, float) else v
                               for k, v in (edge["by_setup"].get(ev["setup"]) or {}).items()},
             "eventi_macro": [{"titolo": m["title"], "ore": round(m["diff_hours"], 1)} for m in macro_events[:6]]}
    system = ("Sei un risk reviewer per un bot di trading crypto spot long-only. Ricevi i numeri di UN ticket gia' "
              "approvato dalle regole. Puoi mettere veto=true solo per rischi concreti visibili nei dati (evento macro "
              "imminente, setup gia' esteso rispetto al VWAP, chop estremo, incoerenza tra stop e volatilita', regime sfavorevole). "
              "Puoi anche modulare leggermente la size con risk_mult tra 0.6 e 1.15. Nel dubbio veto=false, risk_mult=1.0. "
              "Rispondi SOLO con JSON: {\"veto\": true|false, \"risk_mult\": 1.0, \"reason\": \"max 25 parole\"}.")
    try:
        r = SESSION.post("https://api.anthropic.com/v1/messages",
                         headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                         json={"model": CLAUDE_MODEL, "max_tokens": 220, "system": system,
                               "messages": [{"role": "user", "content": json.dumps(facts, ensure_ascii=False)}]}, timeout=20)
        r.raise_for_status()
        txt = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        j = json.loads(m.group(0) if m else txt)
        return {"veto": bool(j.get("veto")),
                "risk_mult": clamp(float(j.get("risk_mult", 1.0) or 1.0), 0.6, 1.15),
                "reason": str(j.get("reason", ""))[:200]}
    except Exception as e:
        log("WARN", f"Revisore Claude non disponibile (si prosegue con le sole regole): {redact(e)}")
        return {"veto": False, "risk_mult": 1.0, "reason": "revisore non raggiungibile"}


def expire_pending(state):
    p = state.get("pending")
    if p and p["expires_ts"] < time.time():
        state["pending"] = None
        state["stats"]["last_action"] = f"Ticket {p['code']} scaduto senza approvazione"


# ==============================================================================
# 11. ESECUZIONE
# ==============================================================================
def status(action, headline, detail, pnl_pct=0.0, pnl_usdt=0.0, r=0.0, alerts=None):
    return {"action_state": action, "headline": headline, "detail": detail, "pnl_pct": pnl_pct, "pnl_usdt": pnl_usdt,
            "current_r": r, "alerts_fired": alerts or []}


def _execute_open(state, ev, usdt, now_str, now_ts, mode, label="EDGE"):
    pos, stats, plan = state["position"], state["stats"], ev["plan"]
    cfg, code, price = ev["coin"], ev["code"], ev["price"]
    buy = live_buy(cfg, usdt, price, mode == "LIVE_PIONEX")
    if not buy["ok"]:
        stats["last_action"] = f"ACQUISTO {code} FALLITO ({now_str})"
        save_state(state)
        return status("ENTRY_FAILED", f"🚨 ACQUISTO {code} NON ESEGUITO", f"{buy['msg']} (nessuna posizione registrata)")
    entry = buy["entry_price"]
    stop, tgt = plan["stop"], plan["target"]
    if stop >= entry:
        stop = entry * (1 - MIN_STOP_PCT / 100.0)
    one_r = entry - stop
    max_loss = buy["invested_usdt"] * (one_r / entry * 100.0 + COST_RT) / 100.0
    pos.update({"is_open": True, "coin_code": code, "symbol": cfg["pionex"], "setup": ev.get("setup") or label.lower(),
                "level": ev.get("level", 0.0), "entry_price": entry, "amount_coin": buy["amount_coin"],
                "invested_usdt": buy["invested_usdt"], "max_loss_usdt": max_loss, "sl_price": stop, "tp_price": tgt,
                "initial_sl": stop, "one_r": one_r, "highest_price": entry, "lowest_price": entry,
                "be_locked": False, "trail_on": False, "time_warned": False, "opened_at": now_str, "opened_ts": now_ts,
                "last_check_ts": now_ts, "close_fail_count": 0, "mode": buy["mode"], "order_id": buy.get("order_id", "SIM"),
                "enter_tag": f"{STRAT_VER}_{ev.get('setup') or label.lower()}"})
    stats["daily_trades"] += 1
    stats["daily_risk_usdt"] = stats.get("daily_risk_usdt", 0.0) + max_loss
    stats["last_action"] = f"APERTO LONG {code} a {fmt_price(entry)} il {now_str} ({label})"
    save_state(state)
    mlp = f" | ML {ev['ml_p']:.2f}" if ev.get("ml_p") is not None else ""
    return status("OPENED_NOW", f"🚀 NUOVA POSIZIONE {label} SU {code} ({buy['mode']})",
                  f"{ev.get('setup') or label}: {buy['amount_coin']} {code} ({buy['invested_usdt']:.2f} USDT) a {fmt_price(entry)} | "
                  f"SL {fmt_price(stop)} | TP {fmt_price(tgt)} ({plan['target_r']:.1f}R) | perdita max {max_loss:.2f} USDT{mlp} | {buy['msg']}",
                  alerts=[f"🔔 Piano fissato prima dell'ingresso: SL {fmt_price(stop)} · TP {fmt_price(tgt)} · time-stop {MAX_HOLD_H}h"])


def _finalize_close(state, code, cfg, price, reason, close, alerts, now_str, now_ts, capital_usdt):
    pos, stats = state["position"], state["stats"]
    entry, pnl, inv = pos["entry_price"], close["pnl_usdt"], pos["invested_usdt"]
    pct = pnl / inv * 100.0 if inv > 0 else 0.0
    r_mult = pnl / pos["max_loss_usdt"] if pos.get("max_loss_usdt", 0) > 0 else pct / 1.7
    one_r = pos.get("one_r") or max(entry * 0.009, 1e-12)
    mfe = (pos.get("highest_price", entry) - entry) / one_r
    mae = (entry - (pos.get("lowest_price") or entry)) / one_r
    stats["total_trades"] += 1
    if pnl >= 0:
        stats["wins"] += 1
        stats["consec_losses"] = 0
    else:
        stats["losses"] += 1
        stats["consec_losses"] += 1
        stats["last_loss_ts"] = now_ts
    stats["total_pnl_pct"] = round(stats["total_pnl_pct"] + pct, 2)
    stats["total_pnl_usdt"] = round(stats["total_pnl_usdt"] + pnl, 2)
    stats["daily_pnl_usdt"] = round(stats["daily_pnl_usdt"] + pnl, 2)
    stats["daily_pnl_pct"] = round(stats["daily_pnl_pct"] + pnl / capital_usdt * 100.0, 2)
    stats["last_close_ts"] = now_ts
    stats.setdefault("coin_last_close", {})[code] = now_ts
    stats["last_action"] = f"CHIUSO {code} ({r_mult:+.2f}R) il {now_str}"
    state["history"].insert(0, {"coin": code, "setup": pos.get("setup", ""), "entry": entry, "exit": close.get("exit_price", price),
                                "pnl_pct": round(pct, 2), "pnl_usdt": round(pnl, 2), "r_mult": round(r_mult, 3),
                                "mfe_r": round(mfe, 2), "mae_r": round(mae, 2), "reason": reason, "closed_at": now_str,
                                "closed_ts": now_ts, "mode": pos.get("mode", "SIMULATION"), "ver": STRAT_VER})
    state["history"] = state["history"][:200]
    recent = [h["r_mult"] for h in state["history"] if h.get("ver") == STRAT_VER and "r_mult" in h][:10]
    extra = []
    if len(recent) >= 8 and sum(recent) / len(recent) < -0.25:
        stats["paused_until_ts"] = now_ts + 12 * 3600
        extra.append("⏸️ Ultimi 10 trade negativi: nuove entrate in pausa per 12h")
    pos["is_open"] = False
    pos["close_fail_count"] = 0
    save_state(state)
    return status("CLOSED_NOW", f"🔔 POSIZIONE {code} CHIUSA", f"{reason} | {r_mult:+.2f}R ({pnl:+.2f} USDT, {pct:+.2f}%) | "
                  f"MFE {mfe:+.1f}R / MAE {-mae:+.1f}R | {close['msg']}", pct, pnl, 0.0, alerts + extra)


def _manage_open_position(state, all_evals, capital_usdt, now_str, now_ts):
    pos = state["position"]
    code = pos["coin_code"]
    cfg = COIN_MAP.get(code, COINS[0])
    ev = all_evals.get(code) or blank_eval(cfg, pos["entry_price"])
    px_live = LIVE_PX.get(cfg["pionex"])
    price = px_live or ev["price"]
    entry, sl, tp = pos["entry_price"], pos["sl_price"], pos["tp_price"]
    one_r = pos.get("one_r") or max(entry - pos.get("initial_sl", sl), entry * 0.009)
    opened_ts = pos.get("opened_ts") or now_ts
    alerts = []

    if pos.get("mode") == "LIVE_PIONEX" and pionex_live_enabled():
        bal = pionex_get_balances()
        if bal is not None:
            held = bal.get(code, {"free": 0.0, "locked": 0.0})
            if held["free"] + held["locked"] < pos["amount_coin"] * 0.90:
                est = pos["amount_coin"] * price * (1 - FEE_SIDE / 100.0) - pos["invested_usdt"]
                return _finalize_close(state, code, cfg, price, "🧾 CHIUSA ESTERNAMENTE (saldo assente)",
                                       {"ok": True, "exit_price": price, "pnl_usdt": est, "msg": "PnL stimato"},
                                       alerts, now_str, now_ts, capital_usdt)
    if not px_live and ev.get("fallback"):
        save_state(state)
        return status("HOLDING", f"⚠️ {code}: PREZZO NON DISPONIBILE", "Gestione SL/TP sospesa fino al prossimo giro.")

    last_chk = pos.get("last_check_ts") or opened_ts
    valid = [c for c in ev.get("c15_recent", []) if c["time"] >= opened_ts]
    fresh = [c for c in valid if c["time"] + 900 > last_chk]
    wick_low = min([c["low"] for c in fresh] + [price])
    wick_high = max([c["high"] for c in fresh] + [price])
    highest = max([pos.get("highest_price", entry), price] + [c["high"] for c in valid])
    lowest = min([pos.get("lowest_price") or entry, price] + [c["low"] for c in valid])
    pos["highest_price"], pos["lowest_price"] = highest, lowest
    sl_at_start = sl
    cur_r = (price - entry) / one_r if one_r > 0 else 0.0
    hours = max(0.0, (now_ts - opened_ts) / 3600.0)

    new_sl = manage_stop(entry, sl, one_r, highest)
    if code != "BTC" and not BTC_REGIME["allow_alts"] and cur_r >= 0.35:
        new_sl = max(new_sl, entry * (1.0 + COST_RT / 100.0))
    if new_sl > sl:
        if (highest - entry) / one_r >= BE_AT_R and not pos["be_locked"]:
            pos["be_locked"] = True
            alerts.append(f"🛡️ +{BE_AT_R:.2f}R raggiunto: stop a break-even netto ({fmt_price(new_sl)})")
        if (highest - entry) / one_r >= TRAIL_START_R and not pos["trail_on"]:
            pos["trail_on"] = True
            alerts.append(f"🚀 +{TRAIL_START_R:.2f}R: trailing attivo (stop {fmt_price(new_sl)})")
        sl = new_sl
        pos["sl_price"] = sl
    if hours >= 0.8 * MAX_HOLD_H and not pos["time_warned"]:
        pos["time_warned"] = True
        alerts.append(f"⏳ {hours:.1f}h aperta: time-stop tra {MAX_HOLD_H - hours:.1f}h")

    reason, fill = None, price
    if wick_low <= sl_at_start or price <= sl:
        kind = "TRAILING/BREAK-EVEN" if sl_at_start >= entry else "STOP LOSS"
        fill = min(price, sl_at_start) if wick_low <= sl_at_start else price
        reason = f"{'🔒' if sl_at_start >= entry else '🛑'} {kind} ({fmt_price(fill)})"
    elif price >= tp or wick_high >= tp:
        fill = max(price, tp) if price >= tp else tp
        reason = f"🎯 TARGET {pos.get('setup', '')} ({fmt_price(fill)})"
    elif hours >= MAX_HOLD_H:
        reason = f"⏱️ TIME-STOP {MAX_HOLD_H}h ({cur_r:+.2f}R)"

    est = pos["amount_coin"] * price * (1 - FEE_SIDE / 100.0) - pos["invested_usdt"]
    pct = est / pos["invested_usdt"] * 100.0 if pos["invested_usdt"] > 0 else 0.0
    if reason:
        close = live_sell(pos, cfg, fill)
        if not close["ok"]:
            pos["close_fail_count"] = pos.get("close_fail_count", 0) + 1
            pos["last_check_ts"] = now_ts
            state["stats"]["last_action"] = f"CHIUSURA {code} FALLITA ({now_str})"
            save_state(state)
            return status("CLOSE_FAILED", f"🚨 CHIUSURA {code} FALLITA - POSIZIONE ANCORA APERTA",
                          f"{reason} | {close['msg']} | tentativo #{pos['close_fail_count']}. Verifica subito su Pionex!",
                          pct, est, cur_r, alerts)
        return _finalize_close(state, code, cfg, fill, reason, close, alerts, now_str, now_ts, capital_usdt)

    pos["last_check_ts"] = now_ts
    save_state(state)
    return status("HOLDING", f"⏳ LONG {code} ({pos['setup']}, {pos['mode']})",
                  f"Entry {fmt_price(entry)} -> {fmt_price(price)} ({pct:+.2f}% netto / {cur_r:+.2f}R) | SL {fmt_price(sl)} | TP {fmt_price(tp)} | {hours:.1f}h",
                  pct, est, cur_r, alerts)


def _try_open_position(state, ranked, edge, macro_events, fng_val, now_str, now_ts):
    expire_pending(state)
    mode, why = trading_mode(state)
    if state.get("pending"):
        p = state["pending"]
        return status("WAITING", f"🕓 TICKET {p['code']} IN ATTESA DI APPROVAZIONE", f"Scade tra {(p['expires_ts'] - time.time()) / 60:.0f} min.")
    cands = [e for e in ranked if e["verdict"]["ok"]]
    if not cands:
        save_state(state)
        best = ranked[0]
        blocks = best["verdict"]["blocks"][:3]
        return status("WAITING", "🛡️ CAPITALE IN USDT: nessun setup con edge ora",
                      f"Migliore: {best['code']} (score {best['score']}) -> {' | '.join(blocks) or 'in attesa'} | modo {mode}")
    best = cands[0]
    rev = claude_review(best, edge, macro_events, fng_val)
    if CLAUDE_REVIEW:
        state["reviews"] = ([{"ts": now_ts, "coin": best["code"], "setup": best["setup"], **rev}] + state.get("reviews", []))[:30]
    if rev["veto"]:
        save_state(state)
        return status("WAITING", f"🤖 VETO CLAUDE su {best['code']}", rev["reason"])
    cap_usable = best["verdict"].get("usable_usdt", best["verdict"]["size_usdt"])
    usdt = round(clamp(best["verdict"]["size_usdt"] * rev.get("risk_mult", 1.0), MIN_ORDER_USDT, cap_usable), 2)
    if REQUIRE_APPROVAL and mode == "LIVE_PIONEX":
        pid = f"{random.randint(0, 0xFFFFFF):06X}"
        state["pending"] = {"id": pid, "code": best["code"], "usdt": usdt, "price": best["price"], "plan": best["plan"],
                            "setup": best["setup"], "level": best["level"], "created_ts": now_ts,
                            "expires_ts": now_ts + PENDING_TTL_MIN * 60}
        save_state(state)
        p = best["plan"]
        return status("PENDING_APPROVAL", f"🎫 TICKET #{pid}: COMPRA {best['code']}?",
                      f"{best['setup']} | {usdt:.2f} USDT a ~{fmt_price(best['price'])} | SL {fmt_price(p['stop'])} | TP {fmt_price(p['target'])} | "
                      f"perdita max {best['verdict']['max_loss_usdt']:.2f} USDT | scade in {PENDING_TTL_MIN} min")
    return _execute_open(state, best, usdt, now_str, now_ts, mode)


def run_manual_command(state, all_evals, eur_usd, command, coin, usdt_req):
    pos = state["position"]
    now_dt = datetime.now(timezone.utc)
    now_str, now_ts = now_dt.strftime("%d/%m %H:%M UTC"), int(now_dt.timestamp())
    capital = INVEST_EUR * eur_usd
    noop = lambda m: status("MANUAL_NOOP", "ℹ️ COMANDO NON ESEGUITO", m)

    def cur_price(code):
        cfg = COIN_MAP[code]
        return LIVE_PX.get(cfg["pionex"]) or (all_evals.get(code) or {}).get("price", 0.0)

    def close_registered():
        code = pos["coin_code"]
        cfg = COIN_MAP.get(code, COINS[0])
        price = cur_price(code)
        close = live_sell(pos, cfg, price)
        if not close["ok"]:
            pos["close_fail_count"] = pos.get("close_fail_count", 0) + 1
            save_state(state)
            return status("CLOSE_FAILED", f"🚨 CHIUSURA MANUALE {code} FALLITA", f"{close['msg']} | Chiudi a mano dall'app Pionex!")
        return _finalize_close(state, code, cfg, price, "🖐️ CHIUSURA MANUALE", close, [], now_str, now_ts, capital)

    if command == "close":
        return close_registered() if pos["is_open"] else noop("Nessuna posizione registrata nel bot.")

    if command == "deny":
        state["pending"] = None
        save_state(state)
        return status("MANUAL_DONE", "🚫 TICKET RIFIUTATO", "Nessun ordine inviato.")

    if command == "approve":
        p = state.get("pending")
        if not p or p["id"] != coin:
            return noop("Ticket non trovato o gia' scaduto.")
        if p["expires_ts"] < now_ts:
            state["pending"] = None
            save_state(state)
            return noop("Ticket scaduto: attendi un nuovo segnale.")
        if pos["is_open"]:
            return noop("C'e' gia' una posizione aperta.")
        price = cur_price(p["code"])
        if price <= p["plan"]["stop"] or price / p["price"] - 1.0 > 0.006:
            state["pending"] = None
            save_state(state)
            return noop("Il prezzo e' cambiato troppo rispetto al ticket: annullato.")
        stub = {"coin": COIN_MAP[p["code"]], "code": p["code"], "price": price, "plan": p["plan"],
                "setup": p["setup"], "level": p["level"]}
        state["pending"] = None
        return _execute_open(state, stub, p["usdt"], now_str, now_ts, "LIVE_PIONEX", label="APPROVATO")

    if command == "sell_all":
        cfg = COIN_MAP.get(coin)
        if not cfg:
            return noop(f"Coin '{coin}' non gestita dal bot.")
        if pos["is_open"] and pos["coin_code"] == coin:
            return close_registered()
        if not pionex_live_enabled():
            return noop("Vendita totale possibile solo in LIVE.")
        close = live_sell({"mode": "LIVE_PIONEX", "amount_coin": 1e12, "invested_usdt": 0.0}, cfg, cur_price(coin))
        if not close["ok"]:
            return status("CLOSE_FAILED", f"🚨 VENDITA TOTALE {coin} FALLITA", f"{close['msg']} | Vendi a mano dall'app Pionex!")
        state["stats"]["last_action"] = f"VENDUTO TUTTO {coin} ({now_str}, manuale)"
        save_state(state)
        return status("MANUAL_DONE", f"🖐️ VENDUTO TUTTO IL SALDO {coin}", f"Incassati {close['pnl_usdt']:+.2f} USDT | {close['msg']}")

    if command == "open":
        if pos["is_open"]:
            return noop(f"Posizione gia' aperta su {pos['coin_code']}: chiudila prima.")
        ev = all_evals.get(coin)
        if not ev or ev.get("fallback"):
            return noop(f"Analisi di '{coin}' non disponibile: riprova.")
        plan = ev["plan"]
        if "stop" not in plan:
            return noop("Piano non calcolabile (dati insufficienti).")
        size, max_loss, _ = size_position(capital, plan)
        usdt = min(usdt_req if usdt_req > 0 else size, capital * (1 - CASH_BUFFER_PCT / 100.0))
        if usdt < MIN_ORDER_USDT:
            return noop(f"Importo {usdt:.2f} USDT sotto il minimo ({MIN_ORDER_USDT}).")
        mode = "LIVE_PIONEX" if pionex_live_enabled() else "SIMULATION"
        res = _execute_open(state, ev, round(usdt, 2), now_str, now_ts, mode, label="MANUALE")
        if not plan["ok"]:
            res["detail"] += f" | ⚠️ piano fuori regole: {plan['reason']}"
        return res
    return noop(f"Comando sconosciuto: '{command}'")


def manage_autonomous_trading(state, ranked, all_evals, edge, eur_usd, macro_events, fng_val):
    now = datetime.now(timezone.utc)
    now_str, now_ts = now.strftime("%d/%m %H:%M UTC"), int(now.timestamp())
    if state["position"]["is_open"]:
        return _manage_open_position(state, all_evals, INVEST_EUR * eur_usd, now_str, now_ts)
    return _try_open_position(state, ranked, edge, macro_events, fng_val, now_str, now_ts)


def trigger_self_chain_dispatch():
    if not SELF_CHAIN_WHEN_OPEN or not GITHUB_TOKEN or not GITHUB_REPO:
        return
    try:
        r = SESSION.post(f"https://api.github.com/repos/{GITHUB_REPO}/dispatches",
                         headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
                         json={"event_type": "tick"}, timeout=8)
        if r.status_code == 204:
            log("INFO", "Self-chain inviato: il prossimo ciclo partira' da solo.")
    except Exception as e:
        log("WARN", f"Self-chain non inviato: {redact(e)}")


# ==============================================================================
# 12. TELEGRAM
# ==============================================================================
_TG_MENU_SET = [False]


def ensure_telegram_commands_menu():
    if _TG_MENU_SET[0] or not TELEGRAM_TOKEN:
        return
    _TG_MENU_SET[0] = True
    cmds = [{"command": "status", "description": "🔄 Stato e classifica"},
            {"command": "close", "description": "🛑 Chiudi la posizione"}] + \
           [{"command": f"open_{c.lower()}", "description": f"🚀 Apri Long {c}"} for c in ("sol", "btc", "eth")]
    try:
        SESSION.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setMyCommands", json={"commands": cmds}, timeout=5)
    except Exception:
        pass


def poll_telegram_commands(state):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return None
    offset = int(state["stats"].get("last_tg_update_id", 0)) + 1
    try:
        r = SESSION.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                        params={"offset": offset, "limit": 15, "timeout": 1}, timeout=5)
        j = r.json() if r.status_code == 200 else {}
        if not j.get("ok"):
            return None
    except Exception:
        return None
    found = None
    for upd in j.get("result", []):
        state["stats"]["last_tg_update_id"] = max(int(upd.get("update_id", 0)), state["stats"].get("last_tg_update_id", 0))
        cb = upd.get("callback_query")
        if cb:
            if str(((cb.get("message") or {}).get("chat") or {}).get("id", "")) == str(TELEGRAM_CHAT_ID):
                parts = (cb.get("data") or "").strip().split(":")
                if parts[0] == "cmd" and len(parts) >= 2:
                    found = {"command": parts[1].lower(), "coin": parts[2].upper() if len(parts) >= 3 else "",
                             "usdt": float(parts[3]) if len(parts) >= 4 and _isnum(parts[3]) else 0.0}
                    try:
                        SESSION.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
                                     json={"callback_query_id": cb["id"], "text": f"✅ {found['command'].upper()} {found['coin']} preso in carico"}, timeout=4)
                    except Exception:
                        pass
            continue
        msg = upd.get("message") or upd.get("edited_message") or {}
        if str((msg.get("chat") or {}).get("id", "")) != str(TELEGRAM_CHAT_ID) or time.time() - int(msg.get("date", 0)) > 900:
            continue
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        parts = text.split()
        cmd = parts[0].lstrip("/").split("@")[0].lower()
        arg = parts[1].upper() if len(parts) >= 2 else ""
        amt = float(parts[2]) if len(parts) >= 3 and _isnum(parts[2]) else 0.0
        if cmd.startswith("open_"):
            cmd, arg = "open", cmd.split("_", 1)[1].upper()
        if cmd in ("close", "chiudi", "panic", "stop"):
            found = {"command": "close", "coin": arg, "usdt": 0.0}
        elif cmd in ("sellall", "sell_all", "venditutto"):
            found = {"command": "sell_all", "coin": arg, "usdt": 0.0}
        elif cmd in ("open", "buy", "apri", "long"):
            found = {"command": "open", "coin": arg, "usdt": amt}
        elif cmd in ("approve", "approva"):
            found = {"command": "approve", "coin": arg, "usdt": 0.0}
        elif cmd in ("deny", "rifiuta"):
            found = {"command": "deny", "coin": arg, "usdt": 0.0}
        elif cmd in ("status", "stato", "report", "start"):
            found = {"command": "status", "coin": "", "usdt": 0.0}
    if found:
        save_state(state)
    return found


def get_public_webapp_url():
    if "/" in GITHUB_REPO:
        owner, repo = GITHUB_REPO.split("/", 1)
        return f"https://{owner.lower()}.github.io/{repo}/"
    return ""


def build_telegram_inline_keyboard(ranked, state):
    pos, rows, top = state["position"], [], []
    url = get_public_webapp_url()
    if url:
        top.append({"text": "📱 Web Console", "url": url})
    if top:
        rows.append(top)
    if state.get("pending"):
        pid = state["pending"]["id"]
        rows.append([{"text": "✅ APPROVA ACQUISTO", "callback_data": f"cmd:approve:{pid}"},
                     {"text": "🚫 Rifiuta", "callback_data": f"cmd:deny:{pid}"}])
    if pos["is_open"]:
        c = pos["coin_code"]
        rows.append([{"text": f"🛑 CHIUDI {c}", "callback_data": f"cmd:close:{c}"},
                     {"text": "🔄 Stato", "callback_data": "cmd:status"}])
    else:
        rows.append([{"text": "🔄 Aggiorna", "callback_data": "cmd:status"}])
        if ranked:
            rows.append([{"text": f"🚀 Manuale {e['code']}", "callback_data": f"cmd:open:{e['code']}"} for e in ranked[:3]])
    return {"inline_keyboard": rows}


def send_telegram_message(text, reply_markup=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    ensure_telegram_commands_menu()
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = SESSION.post(url, json=payload, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            payload.pop("parse_mode")
            payload["text"] = re.sub(r"<[^>]+>", "", text)[:4000]
            SESSION.post(url, json=payload, timeout=HTTP_TIMEOUT)
    except Exception as e:
        log("WARN", f"Errore invio Telegram: {redact(e)}")


def build_telegram_report(ranked, state, ts, ctx):
    pos, stats, edge = state["position"], state["stats"], ctx["edge"]
    mode, why = trading_mode(state)
    pr = strategy_proof(state)
    esc = html_lib.escape
    lines = [f"🤖 <b>PIONEX EDGE BOT {STRAT_VER}</b> ({'🟢 LIVE' if mode == 'LIVE_PIONEX' else '🧪 SIMULAZIONE'})",
             f"🌍 {esc(ctx['session'])} | {esc(BTC_REGIME['label'])} | F&G {ctx['fng']}", "",
             f"<b>{esc(ts['headline'])}</b>", esc(ts["detail"])]
    lines += [f"• {esc(a)}" for a in ts.get("alerts_fired", [])]
    if pos["is_open"]:
        lines += ["", f"📌 <b>{pos['coin_code']}</b> ({esc(pos['setup'])}) entry <code>{fmt_price(pos['entry_price'])}</code> "
                      f"SL <code>{fmt_price(pos['sl_price'])}</code> TP <code>{fmt_price(pos['tp_price'])}</code>"]
    lines += ["", "🏆 <b>TOP 5</b>"]
    for i, e in enumerate(ranked[:5], 1):
        flag = "🟢" if e["verdict"]["ok"] else ("🟡" if e["setup"] else "⚪")
        what = e["setup"] or f"a {e['dist_atr']:.1f} ATR dal max 24h"
        lines.append(f"{i}. {flag} <b>{e['code']}</b> {fmt_price(e['price'])} · score {e['score']} · {esc(what)}")
    if ranked and ranked[0]["setup"] and not ranked[0]["verdict"]["ok"]:
        lines.append("   ↳ " + esc("; ".join(ranked[0]["verdict"]["blocks"][:3])))
    ml = edge["ml"]
    mltxt = (f"attivo (AUC {ml['auc']:.2f}, tenuti {ml['kept_r']:+.2f}R vs {ml['all_r']:+.2f}R)" if ml.get("active")
             else "spento: " + str(ml.get("why", ""))[:70])
    lines += ["", f"🧪 <b>Edge storico</b> ({edge['n']} eventi su ~{edge.get('days', 0)}g, {edge['all']['exp_r']:+.2f}R medio)",
              f"🧠 ML: {esc(mltxt)}"]
    for k, v in edge["by_setup"].items():
        rc = edge["recent_by_setup"].get(k)
        rtxt = f" · {RECENT_DAYS}g {rc['exp_r']:+.2f}R (n={rc['n']})" if rc else ""
        lines.append(f"   {k}: {v['exp_r']:+.2f}R · WR {v['wr']:.0f}% · n={v['n']}{rtxt}")
    fun = stats.get("funnel", {})
    if fun:
        blk = sorted(((k, v) for k, v in fun.items() if k.startswith("BLOCCO")), key=lambda x: -x[1])[:3]
        lines.append(f"🔬 Oggi: {fun.get('valutazioni', 0)} valutazioni · {fun.get('setup_attivo', 0)} setup · {fun.get('passati', 0)} passati")
        lines += [f"   {esc(k.replace('BLOCCO: ', ''))} ×{v}" for k, v in blk]
    wr = stats["wins"] / stats["total_trades"] * 100.0 if stats["total_trades"] else 0.0
    lines += ["", f"📊 {stats['total_trades']} trade ({stats['wins']}V/{stats['losses']}P, WR {wr:.0f}%) · tot {stats['total_pnl_usdt']:+.2f}$ · oggi {stats['daily_pnl_pct']:+.2f}%",
              f"🎓 Prova {STRAT_VER}: {pr['n']}/{PROOF_MIN_TRADES} trade · {pr['exp_r']:+.2f}R · PF {pr['pf']:.2f}"
              + (f" · {esc(why)}" if mode != "LIVE_PIONEX" else " · live consentito")]
    return "\n".join(lines)


def should_send_telegram_now(state, ts, ranked, force=False):
    if force or ts.get("action_state") in ("OPENED_NOW", "CLOSED_NOW", "ENTRY_FAILED", "CLOSE_FAILED", "MANUAL_DONE",
                                            "MANUAL_NOOP", "PENDING_APPROVAL") or ts.get("alerts_fired"):
        return True
    if ranked and ranked[0]["verdict"]["ok"]:
        return True
    return time.time() - int(state["stats"].get("last_report_ts", 0)) >= TELEGRAM_ROUTINE_MIN * 60


# ==============================================================================
# 13. SNAPSHOT PER LA WEB CONSOLE
# ==============================================================================
def write_snapshot(ranked, state, ts, ctx):
    mode, why = trading_mode(state)
    edge = ctx["edge"]
    snap = {"version": STRAT_VER, "updated_ts": int(time.time()), "session": ctx["session"], "mode": mode, "mode_reason": why,
            "btc_regime": BTC_REGIME, "fng": ctx["fng"], "status": ts, "proof": strategy_proof(state),
            "position": state["position"], "stats": state["stats"], "history": state["history"][:40],
            "pending": state.get("pending"), "macro": [{k: m[k] for k in ("title", "impact", "ts")} for m in ctx["macro"]],
            "edge": {"n": edge["n"], "days": edge.get("days", 0), "all": edge["all"], "by_setup": edge["by_setup"],
                     "recent_by_setup": edge.get("recent_by_setup", {}),
                     "by_coin": {f"{k[0]}|{k[1]}": v for k, v in edge["by_coin"].items()},
                     "ml": {k: v for k, v in edge["ml"].items()
                            if k in ("active", "auc", "kept_r", "all_r", "n_oos", "folds_ok", "thr", "why", "n")}},
            "limits": {"risk_per_trade_pct": RISK_PER_TRADE_PCT, "daily_max_loss_pct": DAILY_MAX_LOSS_PCT,
                       "max_trades": MAX_TRADES_PER_DAY, "target_r": TARGET_R, "max_hold_h": MAX_HOLD_H, "cost_rt_pct": COST_RT},
            "coins": [{"code": e["code"], "name": e["coin"]["name"], "price": e["price"], "score": e["score"], "setup": e["setup"],
                       "level": e["level"], "near_level": e["near_level"], "dist_atr": round(e["dist_atr"], 2),
                       "rsi_1h": round(e["rsi_1h"], 1), "adx": round(e["adx"], 1), "er": round(e.get("er", 0.3), 2),
                       "vwap_z": round(e.get("vwap_z", 0.0), 2), "ret24_pct": round(e["ret24"] * 100, 2),
                       "plan": {k: v for k, v in e["plan"].items()}, "ok": e["verdict"]["ok"], "blocks": e["verdict"]["blocks"],
                       "size_usdt": round(e["verdict"].get("size_usdt", 0.0), 2), "tm": e.get("tm"),
                       "ml_p": None if e.get("ml_p") is None else round(e["ml_p"], 3)} for e in ranked]}
    try:
        with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, default=str)
    except Exception as e:
        log("ERROR", f"Snapshot non scritto: {e}")


def sync_live_files_to_github():
    if not os.getenv("GITHUB_ACTIONS"):
        return
    try:
        os.system('git add bot_state.json bot_snapshot.json index.html >/dev/null 2>&1 && '
                  'if ! git diff --cached --quiet; then '
                  'git commit -m "Live update state [skip ci]" >/dev/null 2>&1 && '
                  'git pull --rebase -X theirs origin "${GITHUB_REF_NAME:-main}" >/dev/null 2>&1 || true; '
                  'git push >/dev/null 2>&1 || true; fi')
    except Exception:
        pass


# ==============================================================================
# 14. CICLO COMPLETO, GUARDIAN, BACKTEST CLI
# ==============================================================================
def run_full_cycle(state, manual_override=None, force_telegram=False):
    LIVE_PX.clear()
    LIVE_PX.update(fetch_pionex_prices())
    eur_usd = fetch_eur_usd_rate()
    futures_map = fetch_all_kraken_futures()
    fng_val, _ = fetch_fear_and_greed()
    macro = fetch_macro_calendar()
    all_evals = scan_all_coins(COINS, futures_map)
    pos = state["position"]
    if pos["is_open"] and pos["coin_code"] not in all_evals:
        cfg = COIN_MAP.get(pos["coin_code"], COINS[0])
        all_evals[cfg["code"]] = blank_eval(cfg, LIVE_PX.get(cfg["pionex"]) or pos["entry_price"])
    if not all_evals:
        log("ERROR", "Nessuna coin analizzata (API dati non raggiungibili).")
        return None
    ranked, edge, high_soon = finalize_signals(all_evals, state, eur_usd, macro, fng_val)
    expire_pending(state)
    cmd = manual_override or poll_telegram_commands(state)
    if cmd and cmd.get("command") in ("close", "sell_all", "open", "approve", "deny"):
        ts = run_manual_command(state, all_evals, eur_usd, cmd["command"], cmd.get("coin", ""), cmd.get("usdt", 0.0))
        force_telegram = True
    else:
        force_telegram = force_telegram or bool(cmd and cmd.get("command") == "status")
        ts = manage_autonomous_trading(state, ranked, all_evals, edge, eur_usd, macro, fng_val)
    ctx = {"ranked": ranked, "all_evals": all_evals, "eur_usd": eur_usd, "ts": ts, "fng": fng_val,
           "session": session_label(), "macro": macro, "edge": edge}
    save_state(state)
    write_snapshot(ranked, state, ts, ctx)
    if should_send_telegram_now(state, ts, ranked, force_telegram):
        send_telegram_message(build_telegram_report(ranked, state, ts, ctx), build_telegram_inline_keyboard(ranked, state))
        state["stats"]["last_report_ts"] = int(time.time())
        save_state(state)
    sync_live_files_to_github()
    return ctx


def run_continuous_guardian(state, ctx):
    pos = state["position"]
    if DAEMON_MODE:
        max_seconds = 10 ** 9
    elif ALWAYS_ON_24_7 or pos["is_open"] or state.get("pending"):
        max_seconds = int(GUARDIAN_MINUTES * 60)
    else:
        return
    log("INFO", f"Guardian attivo (tick {GUARDIAN_TICK_SEC}s, rescan {GUARDIAN_RESCAN_SEC}s).")
    start = last_rescan = time.monotonic()
    while time.monotonic() - start < max_seconds:
        time.sleep(max(5, GUARDIAN_TICK_SEC))
        now = datetime.now(timezone.utc)
        now_str, now_ts = now.strftime("%d/%m %H:%M UTC"), int(now.timestamp())
        px = fetch_pionex_prices()
        if px:
            LIVE_PX.update(px)
        expire_pending(state)
        cmd = poll_telegram_commands(state)
        if cmd:
            log("INFO", f"Comando Telegram: {cmd}")
            if cmd["command"] == "status":
                send_telegram_message(build_telegram_report(ctx["ranked"], state, ctx["ts"], ctx),
                                      build_telegram_inline_keyboard(ctx["ranked"], state))
            else:
                st = run_manual_command(state, ctx["all_evals"], ctx["eur_usd"], cmd["command"], cmd.get("coin", ""), cmd.get("usdt", 0.0))
                ctx["ts"] = st
                send_telegram_message(f"<b>{html_lib.escape(st['headline'])}</b>\n{html_lib.escape(st['detail'])}",
                                      build_telegram_inline_keyboard(ctx["ranked"], state))
                write_snapshot(ctx["ranked"], state, st, ctx)
                sync_live_files_to_github()
        if state["position"]["is_open"]:
            st = _manage_open_position(state, ctx["all_evals"], INVEST_EUR * ctx["eur_usd"], now_str, now_ts)
            ctx["ts"] = st
            if st.get("alerts_fired") or st["action_state"] in ("CLOSED_NOW", "CLOSE_FAILED"):
                for al in st.get("alerts_fired", []):
                    send_telegram_message(f"⚡ <b>{html_lib.escape(state['position']['coin_code'])}</b> {html_lib.escape(al)}")
                if st["action_state"] in ("CLOSED_NOW", "CLOSE_FAILED"):
                    send_telegram_message(f"<b>{html_lib.escape(st['headline'])}</b>\n{html_lib.escape(st['detail'])}",
                                          build_telegram_inline_keyboard(ctx["ranked"], state))
                write_snapshot(ctx["ranked"], state, st, ctx)
                sync_live_files_to_github()
        if time.monotonic() - last_rescan >= GUARDIAN_RESCAN_SEC:
            last_rescan = time.monotonic()
            new_ctx = run_full_cycle(state)
            if new_ctx:
                ctx = new_ctx


def run_backtest_cli():
    load_hist_cache()
    series = {}
    for cfg in COINS:
        c = get_hourly_history(cfg)
        if len(c) >= 150:
            series[cfg["code"]] = prep_series(c)
    save_hist_cache()
    if not series:
        print("Nessun dato scaricato (Binance/Kraken non raggiungibili).")
        return
    days = min(len(s["c"]) for s in series.values()) / 24
    btc = series.get("BTC")
    print(f"\nBACKTEST {STRAT_VER}: {len(series)} coin, ~{days:.0f} giorni di candele 1h")
    print(f"Costi: fee {FEE_SIDE}%/lato + slip {SLIP_SIDE}%/lato | time-stop {MAX_HOLD_H}h | BE {BE_AT_R}R\n")
    for tr in (1.5, 2.0, 2.5, 3.0):
        ev = [e for code, S in series.items() for e in backtest_coin(code, S, tr, btc)]
        tot = summarize([e["R"] for e in ev])
        print(f"TARGET {tr:.1f}R -> {tot['n']:4d} trade | expectancy {tot['exp_r']:+.3f}R (±{tot['se']:.2f}) | WR {tot['wr']:4.1f}% | PF {tot['pf']:.2f}")
        for s in SETUPS:
            x = summarize([e["R"] for e in ev if e["setup"] == s])
            if x["n"]:
                print(f"     {s:13s} n={x['n']:4d}  {x['exp_r']:+.3f}R  WR {x['wr']:4.1f}%  PF {x['pf']:.2f}")
    ev = [e for code, S in series.items() for e in backtest_coin(code, S, None, btc)]
    print(f"\nPer coin (TARGET {TARGET_R}R):")
    for code in series:
        x = summarize([e["R"] for e in ev if e["code"] == code])
        print(f"   {code:5s} n={x['n']:4d}  {x['exp_r']:+.3f}R  WR {x['wr']:4.1f}%")
    cut = time.time() - RECENT_DAYS * 86400
    rec = [e["R"] for e in ev if e["t"] >= cut]
    x = summarize(rec)
    print(f"\nUltimi {RECENT_DAYS} giorni: n={x['n']}  {x['exp_r']:+.3f}R  WR {x['wr']:4.1f}%")
    print(f"Frequenza segnali: ~{len(ev) / max(days / 7, 1):.1f} a settimana su {len(series)} coin")
    ml = train_edge_model(ev)
    if ml.get("auc") is not None:
        print(f"\nFiltro ML walk-forward (23 feature): {'ATTIVO' if ml.get('active') else 'NON validato'} "
              f"(AUC {ml['auc']:.2f}, tenuti {ml['kept_r']:+.2f}R vs tutti {ml['all_r']:+.2f}R, "
              f"{ml['folds_ok']}/3 blocchi, n_oos {ml['n_oos']})")
    else:
        print(f"\nFiltro ML: spento ({ml.get('why')})")
    print("\nATTENZIONE: anche 12 mesi sono pochi regimi di mercato. Usa sempre la prova in simulazione prima del live.")


def main():
    if len(sys.argv) > 1 and sys.argv[1].lower() == "backtest":
        run_backtest_cli()
        return
    load_hist_cache()
    state = load_state()
    mode, why = trading_mode(state)
    log("INFO", f"Avvio Pionex Edge Bot {STRAT_VER} | evento {GITHUB_EVENT_NAME} | modo {mode} ({why}) | "
                f"approvazione {REQUIRE_APPROVAL} | Claude review {CLAUDE_REVIEW} | ML {'on' if ML_OK and ML_ENABLED else 'off'}")
    manual = None
    force = GITHUB_EVENT_NAME == "workflow_dispatch"
    if MANUAL_COMMAND in ("close", "sell_all", "open", "status", "approve", "deny"):
        manual = {"command": MANUAL_COMMAND, "coin": MANUAL_COIN, "usdt": MANUAL_USDT}
        force = True
    ctx = run_full_cycle(state, manual_override=manual, force_telegram=force)
    if not ctx:
        return
    run_continuous_guardian(state, ctx)
    if (ALWAYS_ON_24_7 or state["position"]["is_open"]) and not DAEMON_MODE:
        trigger_self_chain_dispatch()


if __name__ == "__main__":
    main()
