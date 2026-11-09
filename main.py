Ho modificato il codice per renderlo **molto più dinamico e propenso all'apertura (meno prudente)**, pur mantenendo i salvavita essenziali per evitare di aprire il bot proprio mentre il mercato sta crollando a picco.

### 📋 Cosa ho modificato per renderlo meno prudente:
1. **Soglia di apertura abbassata:** Ora il bot ti consiglia di aprire già a partire dal punteggio **6/10** (prima serviva 7/10). Inoltre, parte da un punteggio base di +1.
2. **Filtri Notizie molto più rapidi:**
   * Blocco pre-news ridotto da 120 a **30 minuti**.
   * Cooldown post-news ridotto da 45 a **15 minuti** (e da 3 ore a **45 minuti** in caso di dati a sorpresa).
   * **Rimosso** l'obbligo di aspettare la chiusura della candela oraria intera dopo una notizia.
   * Le notizie a "medio impatto" non danno più penalità.
   * L'apertura di Wall Street non blocca più il bot (tetto alzato a 6/10).
3. **Filtro Bitcoin più tollerante:** Le crypto sono volatili. Il veto di BTC ora scatta solo se perde il **-4% in 4h** (prima era -2.2%) o il **-8% in 24h** (prima -5%).
4. **Analisi Tecnica SOL ammorbidita:**
   * **Posizione nel canale:** Ora ti permette di entrare fino all'**80% dell'altezza del range** (prima bloccava a 68%) e non pretende più che l'ultima candela sia per forza verde per darti il massimo dei punti.
   * **Trappola Squeeze (bassa volatilità):** Rimosso il blocco e la penalità. I Grid Bot lavorano bene in laterale, quindi una compressione moderata non blocca più l' operatività.
   * **Volumi rossi:** Tolleranza alzata (fino a -5.5% in 4h prima di far scattare l'allarme balene).
   * **Rendimento griglie:** Abbassato il requisito minimo netto da 0.18% a **0.12%** per griglia, facilitando il punteggio pieno.

Ecco il codice completo pronto da sostituire nel tuo repository GitHub:

```python
from datetime import datetime, timezone
import math
import os
import re
import sys
from zoneinfo import ZoneInfo
import requests


# ---------------------------------------------------------------------
# 1. CONFIGURAZIONE E VARIABILI D'AMBIENTE (TARATURA AGGRESSIVA)
# ---------------------------------------------------------------------

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

ACTIVE_LOWER = os.getenv("ACTIVE_LOWER", "").strip()
ACTIVE_UPPER = os.getenv("ACTIVE_UPPER", "").strip()

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

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

# Tempi di blocco news ridotti drasticamente per operatività più frequente
NEWS_BLOCK_BEFORE_MIN = int(os.getenv("NEWS_BLOCK_BEFORE_MIN", "30"))
NEWS_COOLDOWN_MIN = int(os.getenv("NEWS_COOLDOWN_MIN", "15"))
NEWS_SURPRISE_COOLDOWN_MIN = int(os.getenv("NEWS_SURPRISE_COOLDOWN_MIN", "45"))
NEWS_LOOKBACK_HOURS = int(os.getenv("NEWS_LOOKBACK_HOURS", "2"))
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
    "core pce",
]

# Tolleranza volatilità post-news aumentata
NEWS_MAX_15M_RANGE_PCT = float(os.getenv("NEWS_MAX_15M_RANGE_PCT", "1.8"))
NEWS_MAX_15M_MOVE_PCT = float(os.getenv("NEWS_MAX_15M_MOVE_PCT", "1.5"))

INVEST_EUR = 50
MIN_ORDER_USDT = 5.0
TARGET_STEP_PCT = 0.28  # Più griglie, scalping più veloce
PIONEX_ROUNDTRIP_FEE_PCT = 0.10
MIN_NET_STEP_PCT = 0.12  # Requisito minimo abbassato


# ---------------------------------------------------------------------
# 2. SCALA DECISIONALE DA 1 A 10 (SOGLIA APERTURA A 6/10)
# ---------------------------------------------------------------------

SCORE_ACTIONS = {
    1: {
        "title": "⛔ 1/10 — NON APRIRE NULLA (BLOCCO TOTALE)",
        "desc": "Crollo severo in corso o news FOMC/CPI imminente. Resta in USDT.",
    },
    2: {
        "title": "🔴 2/10 — NON APRIRE (DUMP IN CORSO)",
        "desc": "Bitcoin o Solana in forte vendita. Aspetta che passi la tempesta.",
    },
    3: {
        "title": "🔴 3/10 — SCONSIGLIATO (MERCATO MOLTO DEBOLE)",
        "desc": "Forte downtrend o alta instabilità post-news. Rischio elevato.",
    },
    4: {
        "title": "🟠 4/10 — ATTENDI (CONDIZIONI SFAVOREVOLI)",
        "desc": "Struttura debole o prezzo troppo schiacciato sul tetto del range.",
    },
    5: {
        "title": "🟡 5/10 — NEUTRALE (QUASI PRONTO)",
        "desc": "Mercato incerto o sul bordo alto. Osserva per un piccolo ritracciamento.",
    },
    6: {
        "title": "🟢 6/10 — APRI IL BOT (INGRESSO DINAMICO)",
        "desc": "Condizioni sufficienti! Non è il setup perfetto, ma il mercato è operabile.",
    },
    7: {
        "title": "🟢 7/10 — APRI IL BOT (BUON MOMENTO)",
        "desc": "Filtri verdi e buon potenziale di rotazione. Avvia la griglia.",
    },
    8: {
        "title": "🟢 8/10 — APRI IL BOT (OTTIMO SETUP)",
        "desc": "Mercato sano, Bitcoin collaborativo e prezzo in buona posizione.",
    },
    9: {
        "title": "🔥 9/10 — APRI SENZA ESITARE (ECCELLENTE)",
        "desc": "Allineamento tecnico ottimo. Margine netto elevato e rischio basso.",
    },
    10: {
        "title": "🚀 10/10 — APRI SUBITO IL BOT! (PERFEZIONE)",
        "desc": "Setup d'oro: sconto nel canale, trend solido, BTC calmo e zero news.",
    },
}


# ---------------------------------------------------------------------
# 3. DATI DI MERCATO E INDICATORI
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
        raise RuntimeError(f"Kraken ha restituito candele vuote per {pair}.")

    return {
        "time": [int(float(row[0])) for row in rows],
        "open": [float(row[1]) for row in rows],
        "high": [float(row[2]) for row in rows],
        "low": [float(row[3]) for row in rows],
        "close": [float(row[4]) for row in rows],
        "volume": [float(row[6]) for row in rows],
    }


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


def ema(values, n):
    if not values:
        return 0.0
    k = 2 / (n + 1)
    result = values[0]
    for value in values[1:]:
        result = value * k + result * (1 - k)
    return result


def rsi(closes, n=14):
    if len(closes) <= n:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n

    for gain, loss in zip(gains[n:], losses[n:]):
        avg_gain = (avg_gain * (n - 1) + gain) / n
        avg_loss = (avg_loss * (n - 1) + loss) / n

    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


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

    dx_values = []
    atr_series = []
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
        if pdi + mdi == 0:
            dx_values.append(0.0)
        else:
            dx_values.append(100 * abs(pdi - mdi) / (pdi + mdi))

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
# 4. ANALISI BITCOIN E ANALISI TECNICA SOLANA (PIÙ TOLLERANTE)
# ---------------------------------------------------------------------


def analyze_btc():
    try:
        btc = get_candles(pair="XBTUSD", interval=60)
        close = btc["close"]
        high = btc["high"]
        low = btc["low"]

        price = close[-1]
        chg4h = (price / close[-5] - 1) * 100
        chg24h = (price / close[-25] - 1) * 100
        ema200 = ema(close[-300:], 200)
        _, adx, pdi, mdi, _ = atr_adx_di(high[-250:], low[-250:], close[-250:])

        # Soglie più larghe: interviene solo in caso di vero crollo di BTC
        is_dumping = chg4h <= -4.0 or chg24h <= -8.0
        strong_bear = price < ema200 and mdi > pdi and adx > 34 and chg24h < -3.0

        if is_dumping or strong_bear:
            return {
                "safe": False,
                "cap": 3,
                "status": f"🔴 CROLLO BTC ({price:.0f}$, 4h: {chg4h:+.1f}%, 24h: {chg24h:+.1f}%) — Veto!",
            }
        return {
            "safe": True,
            "cap": 10,
            "status": f"🟢 BTC Operabile ({price:.0f}$, 4h: {chg4h:+.1f}%, 24h: {chg24h:+.1f}%)",
        }
    except Exception as exc:
        return {
            "safe": True,
            "cap": 8,
            "status": f"🟡 Dati BTC non letti ({type(exc).__name__}) — Nessun blocco",
        }


def analyze():
    candles = get_candles(pair="SOLUSD", interval=60)
    opens = candles["open"]
    high = candles["high"]
    low = candles["low"]
    close = candles["close"]
    volume = candles["volume"]

    if len(close) < 250:
        raise RuntimeError(f"Candele insufficienti: {len(close)}/250.")

    price = close[-1]
    ema20 = ema(close[-250:], 20)
    ema50 = ema(close[-250:], 50)
    ema200 = ema(close[-350:], 200)
    rsi14 = rsi(close[-250:])
    atr, adx, pdi, mdi, atr_series = atr_adx_di(
        high[-250:], low[-250:], close[-250:]
    )

    chg4h = (price / close[-5] - 1) * 100
    chg24 = (price / close[-25] - 1) * 100

    avg_atr_7d = (
        sum(atr_series[-168:]) / len(atr_series[-168:]) if atr_series else atr
    )
    atr_ratio = atr / avg_atr_7d if avg_atr_7d > 0 else 1.0
    ema_gap_pct = abs(ema20 - ema50) / price * 100
    is_squeeze = atr_ratio < 0.55 and ema_gap_pct < 0.60

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

    lower = min(max(percentile(low[-168:], 0.08), price * 0.91), price * 0.965)
    upper = max(min(percentile(high[-168:], 0.92), price * 1.09), price * 1.035)

    width_pct = (upper - lower) / price * 100
    invest_usdt = INVEST_EUR * get_eurusd()
    max_grids_by_money = max(1, int(invest_usdt // MIN_ORDER_USDT))
    target_grids = max(5, min(50, round(width_pct / TARGET_STEP_PCT)))
    grids = min(target_grids, max_grids_by_money)

    step_pct = width_pct / grids
    net_step_pct = step_pct - PIONEX_ROUNDTRIP_FEE_PCT
    rotation_ratio = avg_daily / width_pct if width_pct > 0 else 0.0
    stop_loss = lower * 0.975
    position_in_range = (price - lower) / (upper - lower) * 100

    btc_info = analyze_btc()

    # Bonus base di +1 per rendere il bot più propenso all'azione
    technical_score = 1
    reasons = []
    risk_vetoes = []
    tech_cap = 10

    if not btc_info["safe"]:
        tech_cap = min(tech_cap, btc_info["cap"])
        risk_vetoes.append(btc_info["status"])

    # PILASTRO 1: Direzione Trend & EMA 200 (Più permissivo)
    is_downtrend = price < ema200 and mdi > pdi
    if is_downtrend and (adx > 30 and chg24 < -6.5):
        reasons.append(
            f"🔴 1. Forte Downtrend: Sotto EMA200 ({ema200:.1f}$) e ADX {adx:.0f} (+0 pt)"
        )
        tech_cap = min(tech_cap, 4)
        risk_vetoes.append("🚨 Veto Trend: SOL in pesante ribasso.")
    elif adx < 26 and not (is_downtrend and chg24 < -4.0):
        technical_score += 2
        reasons.append(
            f"🟢 1. Struttura Trend: Ottima per Grid (ADX {adx:.1f}) (+2 pt)"
        )
    elif adx < 34 or price >= ema200:
        technical_score += 1
        reasons.append(
            f"🟡 1. Struttura Trend: Operabile (ADX {adx:.1f}, EMA200 {ema200:.1f}$) (+1 pt)"
        )
    else:
        reasons.append(
            f"🔴 1. Struttura Trend: Mercato direzionale ribassista (ADX {adx:.1f}) (+0 pt)"
        )

    # PILASTRO 2: Volatilità (Rimosso il blocco Squeeze)
    if 0.60 <= atr_ratio <= 1.65 and ema_gap_pct < 2.8:
        technical_score += 2
        reasons.append(
            f"🟢 2. Volatilità: Ideale per griglia (ATR {atr_ratio:.2f}x) (+2 pt)"
        )
    elif is_squeeze:
        technical_score += 1
        reasons.append(
            f"🟡 2. Volatilità: Compressa/Squeeze ({atr_ratio:.2f}x), accumulo laterale (+1 pt)"
        )
    elif 0.50 <= atr_ratio <= 2.00:
        technical_score += 1
        reasons.append(
            f"🟡 2. Volatilità: Accettabile (ATR {atr_ratio:.2f}x) (+1 pt)"
        )
    else:
        reasons.append(
            f"🔴 2. Volatilità: Estrema (ATR {atr_ratio:.2f}x) (+0 pt)"
        )

    # PILASTRO 3: Posizione nel Canale (Fino all'80% senza blocchi duri)
    if position_in_range > 82:
        tech_cap = min(tech_cap, 5)
        reasons.append(
            f"🔴 3. Posizione Range ({position_in_range:.0f}%): Troppo vicino al tetto (+0 pt)"
        )
        risk_vetoes.append("⚠️ Prezzo sul tetto del range (>82%).")
    elif 12 <= position_in_range <= 68 and 28 <= rsi14 <= 66:
        technical_score += 2
        reasons.append(
            f"🟢 3. Posizione Range: Ottima zona ({position_in_range:.0f}%), RSI {rsi14:.0f} (+2 pt)"
        )
    elif 5 <= position_in_range <= 82:
        technical_score += 1
        reasons.append(
            f"🟡 3. Posizione Range: Accettabile ({position_in_range:.0f}%), RSI {rsi14:.0f} (+1 pt)"
        )
    else:
        reasons.append(
            f"🔴 3. Posizione Range: Estrema ({position_in_range:.0f}%) (+0 pt)"
        )

    # PILASTRO 4: Volumi & Smart Money (Soglie di allarme più alte)
    if max_red_vol_ratio >= 3.2 or chg4h <= -5.5:
        technical_score -= 1
        tech_cap = min(tech_cap, 3)
        reasons.append(
            f"🔴 4. Allarme Dump: Vendite {max_red_vol_ratio:.1f}x media (4h: {chg4h:+.1f}%) (-1 pt)"
        )
        risk_vetoes.append("🚨 Veto Dump: Crollo rapido nelle ultime 4 ore.")
    elif max_red_vol_ratio < 1.8:
        technical_score += 2
        reasons.append(
            f"🟢 4. Volumi: Regolari, nessuna pressione anomala ({max_red_vol_ratio:.1f}x) (+2 pt)"
        )
    else:
        technical_score += 1
        reasons.append(
            f"🟡 4. Volumi: Vendite in aumento ({max_red_vol_ratio:.1f}x) ma gestibili (+1 pt)"
        )

    # PILASTRO 5: Rendimento Netto Fee & Rotazione (Più facile prendere +2)
    if net_step_pct >= MIN_NET_STEP_PCT and rotation_ratio >= 0.30:
        technical_score += 2
        reasons.append(
            f"🟢 5. Rendimento: +{net_step_pct:.2f}% netto | Rotazione {rotation_ratio:.2f}x/gg (+2 pt)"
        )
    elif net_step_pct >= 0.08 and rotation_ratio >= 0.20:
        technical_score += 1
        reasons.append(
            f"🟡 5. Rendimento: +{net_step_pct:.2f}% netto | Rotazione {rotation_ratio:.2f}x/gg (+1 pt)"
        )
    else:
        reasons.append(
            f"🔴 5. Rendimento: Margine stretto (+{net_step_pct:.2f}%) (+0 pt)"
        )

    raw_tech_score = max(1, min(10, technical_score))
    capped_tech_score = min(raw_tech_score, tech_cap)

    return {
        "price": price,
        "ema200": ema200,
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
        "raw_tech_score": raw_tech_score,
        "score": capped_tech_score,
        "tech_cap": tech_cap,
        "reasons": reasons,
        "risk_vetoes": risk_vetoes,
        "btc_info": btc_info,
        "invest_usdt": invest_usdt,
        "candle_times": candles["time"],
    }


# ---------------------------------------------------------------------
# 5. FUNZIONI DI RACCOLTA CALENDARIO ECONOMICO
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
        return rel_pct >= 20.0, rel_pct  # Alzata soglia sorpresa al 20%
    return diff >= 0.3, diff * 100


def is_wall_street_open_window(now_utc):
    ny_time = now_utc.astimezone(NY_TZ)
    if ny_time.weekday() >= 5:
        return False, ny_time.strftime("%H:%M")
    minutes_of_day = ny_time.hour * 60 + ny_time.minute
    in_window = 565 <= minutes_of_day <= 600  # Ridotta a 9:25 - 10:00 NY
    return in_window, ny_time.strftime("%H:%M")


def fetch_macro_events(now):
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; GridBot/3.0)",
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
        if reaction_pct > 0.35
        else ("ribassista 📉" if reaction_pct < -0.35 else "neutra ➖")
    )

    return {
        "reaction_pct": reaction_pct,
        "move_15m_pct": move_15m_pct,
        "range_15m_pct": range_15m_pct,
        "stable": stable,
        "direction": direction,
    }


# ---------------------------------------------------------------------
# 6. VALUTAZIONE NEWS (MENO RESTRITTIVA) E INVIO TELEGRAM
# ---------------------------------------------------------------------


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


def get_news_context(hourly_times):
    now = datetime.now(timezone.utc)
    ws_open_active, ny_time_str = is_wall_street_open_window(now)

    try:
        events = fetch_macro_events(now)
    except Exception as exc:
        # Se il calendario fallisce, non blocca più a 1/10 ma avvisa solamente
        return {
            "ok": False,
            "evaluated_lines": [f"⚠️ Calendario offline: {exc}"],
            "explanation": "Calendario non raggiungibile. Nessun blocco forzato (Max 8/10).",
            "penalty": 0,
            "max_score_cap": 8,
            "ws_status": f"NY Time: {ny_time_str}",
        }

    evaluated_lines = [format_event_line(e) for e in events[:10]]
    if not evaluated_lines:
        evaluated_lines = ["✅ Nessuna notizia rilevante nelle vicinanze."]

    high_impact = [e for e in events if e["impact"] == "high"]

    # Apertura WS non blocca più il bot (cap a 6 anziché 3)
    if ws_open_active and high_impact:
        return {
            "ok": True,
            "evaluated_lines": evaluated_lines,
            "explanation": f"🔔 Apertura Wall Street ({ny_time_str} NY) con news in giornata: leggera cautela (Max 6/10).",
            "penalty": 0,
            "max_score_cap": 6,
            "ws_status": f"🟡 Apertura NY ({ny_time_str})",
        }

    upcoming_high = [
        e for e in high_impact if 0 <= e["mins_diff"] <= NEWS_BLOCK_BEFORE_MIN
    ]
    if upcoming_high:
        ev = min(upcoming_high, key=lambda e: e["dt"])
        mins = math.ceil(ev["mins_diff"])
        cap = 2 if (mins <= 15 and ev["is_critical"]) else 4
        return {
            "ok": True,
            "evaluated_lines": evaluated_lines,
            "explanation": f"Pre-News: '{ev['name']}' tra {mins} min. Punteggio limitato a max {cap}/10.",
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
                surp_txt = " (Sorpresa!)" if ev["is_surprise"] else ""
                return {
                    "ok": True,
                    "evaluated_lines": evaluated_lines,
                    "explanation": f"Cooldown rapido{surp_txt}: '{ev['name']}' uscita da {round(elapsed)}m. Attendi {rem}m (Max 4/10).",
                    "penalty": 0,
                    "max_score_cap": 4,
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
                    "explanation": f"{reac_text} ⚠️ Volatilità post-news ancora alta (Max 5/10).",
                    "penalty": 0,
                    "max_score_cap": 5,
                    "ws_status": f"🟢 NY Time: {ny_time_str}",
                }
            return {
                "ok": True,
                "evaluated_lines": evaluated_lines,
                "explanation": f"{reac_text} ✅ Notizia assorbita.",
                "penalty": 0,
                "max_score_cap": 10,
                "ws_status": f"🟢 NY Time: {ny_time_str}",
            }

    return {
        "ok": True,
        "evaluated_lines": evaluated_lines,
        "explanation": "✅ Via libera macroeconomico (0 penalità).",
        "penalty": 0,
        "max_score_cap": 10,
        "ws_status": f"🟢 NY Time: {ny_time_str}",
    }


def make_progress_bar(score):
    fill = "🟩" if score >= 6 else ("🟨" if score >= 4 else "🟥")
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
        f"🤖 AI GRID ADVISOR SOL/USDT (FAST) — {now_str}",
        f"💰 SOL: {analysis['price']:.2f}$ (4h: {analysis['chg4h']:+.1f}% | 24h: {analysis['chg24']:+.1f}%)",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📊 PUNTEGGIO FINALE: {final_score}/10",
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
            "🛡️ STATO DI MERCATO:",
            f"• {analysis['btc_info']['status']}",
            f"• Trend EMA200: {'🟢 Sopra' if analysis['price'] >= analysis['ema200'] else '🟠 Sotto'} ({analysis['ema200']:.1f}$) | +DI:{analysis['pdi']:.0f} vs -DI:{analysis['mdi']:.0f}",
            f"• Wall Street: {news['ws_status']}",
        ]
    )

    if analysis["risk_vetoes"]:
        for veto in analysis["risk_vetoes"]:
            lines.append(f"• {veto}")
    else:
        lines.append("• ✅ Nessun blocco critico attivo.")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    if final_score >= 6:
        lines.append("🛠️ PARAMETRI PIONEX CONSIGLIATI:")
    else:
        lines.append("🛠️ PARAMETRI IPOTETICI (ATTENDERE):")

    lines.extend(
        [
            f"• Range: {analysis['lower']:.2f}$ — {analysis['upper']:.2f}$ (Pos: {analysis['pos']:.0f}%)",
            f"• Griglie: {analysis['grids']} (Lordo: {analysis['step_pct']:.2f}% | Netto: +{analysis['net_step_pct']:.2f}%)",
            f"• Stop Loss: {analysis['stop']:.2f}$",
            f"• Budget: ~{analysis['invest_usdt']:.0f} USDT",
        ]
    )

    lines.extend(
        [
            "━━━━━━━━━━━━━━━━━━━━",
            f"🧠 VALUTAZIONE TECNICA (Punti: {analysis['raw_tech_score']}/10 | Tetto: {analysis['tech_cap']}/10):",
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

    news = get_news_context(analysis.get("candle_times"))
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
