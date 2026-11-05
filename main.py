import os
import sys
import math
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


# ---------------------------------------------------------------------
# CONFIGURAZIONE
# ---------------------------------------------------------------------

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Range di una griglia già attiva su Pionex, facoltativo.
ACTIVE_LOWER = os.getenv("ACTIVE_LOWER", "").strip()
ACTIVE_UPPER = os.getenv("ACTIVE_UPPER", "").strip()

# Feed pubblico del calendario economico.
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

COUNTRY_ALIASES = {
    "us": "usd", "usa": "usd", "united states": "usd",
    "eu": "eur", "eurozone": "eur", "european union": "eur",
    "uk": "gbp", "gb": "gbp", "united kingdom": "gbp",
    "japan": "jpy", "jp": "jpy", "switzerland": "chf", "ch": "chf",
    "canada": "cad", "ca": "cad", "australia": "aud", "au": "aud",
    "new zealand": "nzd", "nz": "nzd", "china": "cny", "cn": "cny",
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
    print(f"⚠️ Fuso orario {LOCAL_TZ_NAME!r} non valido; uso UTC.")
    LOCAL_TZ = timezone.utc
    LOCAL_TZ_NAME = "UTC"

# Filtro calendario.
NEWS_BLOCK_BEFORE_MIN = int(os.getenv("NEWS_BLOCK_BEFORE_MIN", "120"))
NEWS_COOLDOWN_MIN = int(os.getenv("NEWS_COOLDOWN_MIN", "45"))
NEWS_LOOKBACK_HOURS = int(os.getenv("NEWS_LOOKBACK_HOURS", "4"))
NEWS_LOOKAHEAD_HOURS = int(os.getenv("NEWS_LOOKAHEAD_HOURS", "12"))

# Parole chiave che rendono una news estremamente pericolosa per le crypto
CRITICAL_KEYWORDS = [
    "fomc", "cpi", "powell", "interest rate", "non-farm", 
    "nfp", "fed ", "federal funds", "ppi", "gdp", "core pce"
]

# Soglie di stabilità post-news su SOL.
NEWS_MAX_15M_RANGE_PCT = float(os.getenv("NEWS_MAX_15M_RANGE_PCT", "1.0"))
NEWS_MAX_15M_MOVE_PCT = float(os.getenv("NEWS_MAX_15M_MOVE_PCT", "0.8"))

# Impostazioni della griglia.
INVEST_EUR = 50
MIN_ORDER_USDT = 5.0
TARGET_STEP_PCT = 0.30
MIN_STEP_PCT = 0.25

# ---------------------------------------------------------------------
# SCALA DECISIONALE 1 - 10
# ---------------------------------------------------------------------

SCORE_ACTIONS = {
    1: {
        "title": "⛔ 1/10 — NON APRIRE NULLA (BLOCCO TOTALE)",
        "desc": "Mercato ad altissimo rischio, errore dati o news critica imminente. Resta in USDT."
    },
    2: {
        "title": "🔴 2/10 — NON APRIRE (PERICOLO ELEVATO)",
        "desc": "News appena rilasciata o mercato in forte tempesta. Attendi che le acque si calmino."
    },
    3: {
        "title": "🔴 3/10 — SCONSIGLIATO (MERCATO OSTILE)",
        "desc": "Forte trend direzionale o instabilità post-news. Un Grid Bot andrebbe subito fuori range."
    },
    4: {
        "title": "🟠 4/10 — ATTENDI (CONDIZIONI DEBOLI)",
        "desc": "Struttura tecnica scarsa o news in avvicinamento. Non forzare l'ingresso, aspetta."
    },
    5: {
        "title": "🟡 5/10 — NEUTRALE (MEGLIO ASPETTARE)",
        "desc": "Mercato indeciso (50/50). Il rapporto rischio/rendimento non giustifica l'apertura ora."
    },
    6: {
        "title": "🟡 6/10 — QUASI PRONTO (OSSERVA)",
        "desc": "Il mercato sta iniziando a lateralizzare, ma manca ancora qualche conferma tecnica."
    },
    7: {
        "title": "🟢 7/10 — VALUTA APERTURA (CON PRUDENZA)",
        "desc": "Buone condizioni. Puoi aprire il bot, magari tenendo il range leggermente più largo."
    },
    8: {
        "title": "🟢 8/10 — APRI IL BOT (BUON MOMENTO)",
        "desc": "Mercato laterale sano e privo di ostacoli macro imminenti. Segui i parametri suggeriti."
    },
    9: {
        "title": "🔥 9/10 — APRI SENZA ESITARE (OTTIMO SETUP)",
        "desc": "Indicatori tecnici quasi perfetti e via libera dalle news. Condizioni ideali per Pionex."
    },
    10: {
        "title": "🚀 10/10 — APRI SUBITO IL BOT! (PERFEZIONE)",
        "desc": "Allineamento perfetto: zero trend, prezzo a metà canale, volatilità ideale e zero news ostili."
    }
}


# ---------------------------------------------------------------------
# DATI DI MERCATO
# ---------------------------------------------------------------------

def get_candles(interval=60):
    response = requests.get(
        "https://api.kraken.com/0/public/OHLC",
        params={"pair": "SOLUSD", "interval": interval},
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()

    if data.get("error"):
        raise RuntimeError(f"Kraken: {data['error']}")

    result = data.get("result", {})
    key = next((name for name in result if name != "last"), None)

    if key is None:
        raise RuntimeError("Kraken non ha restituito candele.")

    rows = result[key][:-1]
    if not rows:
        raise RuntimeError("Kraken ha restituito candele vuote.")

    return {
        "time": [int(float(row[0])) for row in rows],
        "high": [float(row[2]) for row in rows],
        "low": [float(row[3]) for row in rows],
        "close": [float(row[4]) for row in rows],
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


# ---------------------------------------------------------------------
# INDICATORI
# ---------------------------------------------------------------------

def ema(values, n):
    k = 2 / (n + 1)
    result = values[0]
    for value in values[1:]:
        result = value * k + result * (1 - k)
    return result


def rsi(closes, n=14):
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


def atr_adx(high, low, close, n=14):
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

    for i in range(n, len(true_ranges) + 1):
        if i > n:
            smoothed_tr = smoothed_tr - smoothed_tr / n + true_ranges[i - 1]
            smoothed_p = smoothed_p - smoothed_p / n + positive_dm[i - 1]
            smoothed_m = smoothed_m - smoothed_m / n + negative_dm[i - 1]

        if smoothed_tr <= 0:
            dx_values.append(0.0)
            continue

        pdi = 100 * smoothed_p / smoothed_tr
        mdi = 100 * smoothed_m / smoothed_tr
        dx_values.append(0.0 if pdi + mdi == 0 else 100 * abs(pdi - mdi) / (pdi + mdi))

    if not dx_values:
        return 0.0, 0.0

    seed = dx_values[:n]
    adx_value = sum(seed) / len(seed)
    for dx in dx_values[n:]:
        adx_value = (adx_value * (n - 1) + dx) / n

    return smoothed_tr / n, adx_value


def percentile(values, p):
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * p))))
    return ordered[index]


# ---------------------------------------------------------------------
# ANALISI TECNICA E SPIEGAZIONE PUNTEGGIO
# ---------------------------------------------------------------------

def analyze():
    candles = get_candles(interval=60)
    high, low, close = candles["high"], candles["low"], candles["close"]

    if len(close) < 200:
        raise RuntimeError(f"Candele insufficienti: {len(close)}/200.")

    price = close[-1]
    ema20 = ema(close[-200:], 20)
    ema50 = ema(close[-200:], 50)
    rsi14 = rsi(close[-200:])
    atr, adx = atr_adx(high[-200:], low[-200:], close[-200:])
    atr_pct = atr / price * 100

    chg24 = (price / close[-25] - 1) * 100
    chg7d = (price / close[-169] - 1) * 100

    daily_ranges = [
        (max(high[-(d + 1) * 24: len(high) - d * 24]) - min(low[-(d + 1) * 24: len(low) - d * 24])) / price * 100
        for d in range(7)
    ]
    avg_daily = sum(daily_ranges) / len(daily_ranges)

    lower = min(max(percentile(low[-168:], 0.10), price * 0.92), price * 0.965)
    upper = max(min(percentile(high[-168:], 0.90), price * 1.08), price * 1.035)

    width_pct = (upper - lower) / price * 100
    invest_usdt = INVEST_EUR * get_eurusd()
    max_grids_by_money = max(1, int(invest_usdt // MIN_ORDER_USDT))
    target_grids = max(5, min(50, round(width_pct / TARGET_STEP_PCT)))
    grids = min(target_grids, max_grids_by_money)
    step_pct = width_pct / grids
    stop_loss = lower * 0.975
    position_in_range = (price - lower) / (upper - lower) * 100

    # --- CALCOLO PUNTEGGIO CON SPIEGAZIONE ---
    technical_score = 0
    reasons = []
    ema_gap_pct = abs(ema20 - ema50) / price * 100

    # 1. ADX (Forza del trend)
    if adx < 20:
        technical_score += 2
        reasons.append(f"🟢 ADX ({adx:.1f} < 20): Assenza di trend, mercato laterale ideale (+2 pt)")
    elif adx < 25:
        technical_score += 1
        reasons.append(f"🟡 ADX ({adx:.1f} < 25): Trend debole, accettabile (+1 pt)")
    else:
        reasons.append(f"🔴 ADX ({adx:.1f} ≥ 25): Trend direzionale troppo forte (+0 pt)")

    # 2. Distanza EMA20 / EMA50
    if ema_gap_pct < 1.0:
        technical_score += 2
        reasons.append(f"🟢 Gap EMA ({ema_gap_pct:.2f}%): Medie mobili piatte e vicine (+2 pt)")
    elif ema_gap_pct < 3.0:
        technical_score += 1
        reasons.append(f"🟡 Gap EMA ({ema_gap_pct:.2f}%): Leggera divergenza tra le medie (+1 pt)")
    else:
        reasons.append(f"🔴 Gap EMA ({ema_gap_pct:.2f}%): Medie troppo distanti, mercato sbilanciato (+0 pt)")

    # 3. RSI (14)
    if 40 <= rsi14 <= 60:
        technical_score += 2
        reasons.append(f"🟢 RSI ({rsi14:.1f}): Perfettamente neutro al centro (+2 pt)")
    elif 30 <= rsi14 <= 70:
        technical_score += 1
        reasons.append(f"🟡 RSI ({rsi14:.1f}): Zona accettabile ma non centrale (+1 pt)")
    else:
        reasons.append(f"🔴 RSI ({rsi14:.1f}): Mercato in ipercomprato/ipervenduto estremo (+0 pt)")

    # 4. Posizione nel Range
    if 35 <= position_in_range <= 65:
        technical_score += 2
        reasons.append(f"🟢 Posizione ({position_in_range:.0f}%): Prezzo al centro del canale (+2 pt)")
    elif 25 <= position_in_range <= 75:
        technical_score += 1
        reasons.append(f"🟡 Posizione ({position_in_range:.0f}%): Prezzo leggermente decentrato (+1 pt)")
    else:
        reasons.append(f"🔴 Posizione ({position_in_range:.0f}%): Prezzo troppo vicino ai bordi del range (+0 pt)")

    # 5. Volatilità Giornaliera Media (7d)
    if 3.0 <= avg_daily <= 8.0:
        technical_score += 2
        reasons.append(f"🟢 Volatilità 7d ({avg_daily:.1f}%): Oscillazione perfetta per fare profitti (+2 pt)")
    elif 1.5 <= avg_daily < 3.0:
        technical_score += 1
        reasons.append(f"🟡 Volatilità 7d ({avg_daily:.1f}%): Mercato un po' lento (+1 pt)")
    else:
        reasons.append(f"🔴 Volatilità 7d ({avg_daily:.1f}%): Eccessiva o quasi nulla (+0 pt)")

    # Penalità
    if abs(chg24) > 8.0:
        technical_score -= 2
        reasons.append(f"⚠️ Penalità Pump/Dump: Variazione 24h eccessiva ({chg24:+.1f}%) (-2 pt)")

    if step_pct < MIN_STEP_PCT:
        technical_score -= 1
        reasons.append(f"⚠️ Penalità Griglia: Profitto per griglia stretto ({step_pct:.2f}%) (-1 pt)")

    raw_score = technical_score
    technical_score = max(1, min(10, technical_score))

    return {
        "price": price,
        "chg24": chg24,
        "chg7d": chg7d,
        "lower": lower,
        "upper": upper,
        "grids": grids,
        "step_pct": step_pct,
        "stop": stop_loss,
        "pos": position_in_range,
        "score": technical_score,
        "raw_score": raw_score,
        "reasons": reasons,
        "invest_usdt": invest_usdt,
        "candle_times": candles["time"],
    }


# ---------------------------------------------------------------------
# CALENDARIO ECONOMICO ED ANALISI NEWS APPROFONDITA
# ---------------------------------------------------------------------

def parse_calendar_time(value):
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    event_time = datetime.fromisoformat(raw)
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)
    return event_time.astimezone(timezone.utc)


def fetch_macro_events(now):
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; GridBot/2.0)",
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

        # Prendiamo eventi nelle ultime X ore e nelle prossime Y ore
        if not (-NEWS_LOOKBACK_HOURS * 60 <= minutes_to_event <= NEWS_LOOKAHEAD_HOURS * 60):
            continue

        title = str(item.get("title") or item.get("event") or "Evento")
        is_critical = any(kw in title.lower() for kw in CRITICAL_KEYWORDS)

        events.append({
            "dt": event_time,
            "local_dt": event_time.astimezone(LOCAL_TZ),
            "country": country.upper(),
            "impact": impact,
            "is_critical": is_critical,
            "name": title,
            "actual": str(item.get("actual") or "").strip(),
            "estimate": str(item.get("forecast") or item.get("estimate") or "").strip(),
            "previous": str(item.get("previous") or item.get("prev") or "").strip(),
            "mins_diff": minutes_to_event
        })

    events.sort(key=lambda e: e["dt"])
    return events


def get_post_news_reaction(event):
    candles = get_candles(interval=5)
    times, closes = candles["time"], candles["close"]
    if len(closes) < 4:
        return None

    interval_seconds = 300
    release_ts = event["dt"].timestamp()
    first_post_ts = math.ceil(release_ts / interval_seconds) * interval_seconds

    post_indices = [i for i, ts in enumerate(times) if ts >= first_post_ts]
    pre_indices = [i for i, ts in enumerate(times) if ts + interval_seconds <= release_ts]

    if not pre_indices or len(post_indices) < 4:
        return None

    last_four = post_indices[-4:]
    if any(times[b] - times[a] != interval_seconds for a, b in zip(last_four, last_four[1:])):
        return None

    if datetime.now(timezone.utc).timestamp() - (times[-1] + interval_seconds) > 900:
        return None

    pre_price = closes[pre_indices[-1]]
    current_price = closes[last_four[-1]]
    reaction_pct = (current_price / pre_price - 1) * 100
    move_15m_pct = (closes[last_four[-1]] / closes[last_four[0]] - 1) * 100

    last_three = last_four[-3:]
    high_15m = max(candles["high"][i] for i in last_three)
    low_15m = min(candles["low"][i] for i in last_three)
    range_15m_pct = (high_15m - low_15m) / current_price * 100

    stable = (abs(move_15m_pct) <= NEWS_MAX_15M_MOVE_PCT and range_15m_pct <= NEWS_MAX_15M_RANGE_PCT)
    direction = "rialzista 📈" if reaction_pct > 0.30 else ("ribassista 📉" if reaction_pct < -0.30 else "neutra ➖")

    return {
        "reaction_pct": reaction_pct,
        "move_15m_pct": move_15m_pct,
        "range_15m_pct": range_15m_pct,
        "stable": stable,
        "direction": direction,
    }


def format_event_line(ev):
    icon = "🔥" if ev["is_critical"] else ("🔴" if ev["impact"] == "high" else "🟠")
    time_str = ev["local_dt"].strftime("%H:%M")
    mins = round(ev["mins_diff"])
    
    if mins > 0:
        timing = f"tra {mins} min"
    else:
        timing = f"{abs(mins)} min fa"

    data_str = ""
    if ev["actual"] or ev["estimate"]:
        act = ev["actual"] if ev["actual"] else "In attesa"
        est = f" (Prev: {ev['estimate']})" if ev["estimate"] else ""
        data_str = f" — Dati: {act}{est}"

    return f"{icon} [{time_str}] {ev['country']} {ev['name']} ({timing}){data_str}"


def get_news_context(hourly_times):
    now = datetime.now(timezone.utc)
    try:
        events = fetch_macro_events(now)
    except Exception as exc:
        return {
            "ok": False,
            "state": "unknown",
            "error_msg": str(exc),
            "events": [],
            "evaluated_lines": [f"❌ Errore caricamento calendario: {exc}"],
            "explanation": "Impossibile verificare le notizie macroeconomiche. Per sicurezza il punteggio viene bloccato a 1.",
            "penalty": 0,
            "max_score_cap": 1,
        }

    evaluated_lines = [format_event_line(e) for e in events]
    if not evaluated_lines:
        evaluated_lines = ["✅ Nessuna notizia ad alto/medio impatto nelle vicinanze."]

    high_impact = [e for e in events if e["impact"] == "high"]
    med_impact = [e for e in events if e["impact"] == "medium"]

    # 1. Controllo Pre-News Alto Impatto
    upcoming_high = [e for e in high_impact if 0 <= e["mins_diff"] <= NEWS_BLOCK_BEFORE_MIN]
    if upcoming_high:
        ev = min(upcoming_high, key=lambda e: e["dt"])
        mins = math.ceil(ev["mins_diff"])
        cap = 1 if (mins <= 30 or ev["is_critical"]) else 3
        return {
            "ok": True,
            "state": "before_high",
            "events": events,
            "evaluated_lines": evaluated_lines,
            "explanation": f"Blocco Pre-News: '{ev['name']}' uscirà tra {mins} minuti. Punteggio bloccato a max {cap}/10.",
            "penalty": 0,
            "max_score_cap": cap,
        }

    # 2. Controllo Cooldown (News appena uscita)
    recent_high = [e for e in high_impact if -NEWS_COOLDOWN_MIN < e["mins_diff"] <= 0]
    if recent_high:
        ev = max(recent_high, key=lambda e: e["dt"])
        elapsed = abs(round(ev["mins_diff"]))
        rem = math.ceil(NEWS_COOLDOWN_MIN - elapsed)
        return {
            "ok": True,
            "state": "cooldown",
            "events": events,
            "evaluated_lines": evaluated_lines,
            "explanation": f"Cooldown attivo: '{ev['name']}' è uscita da {elapsed} min. Attendi ancora {rem} min. Punteggio bloccato a 2/10.",
            "penalty": 0,
            "max_score_cap": 2,
        }

    # 3. Controllo Post-News (Verifica stabilità SOL)
    post_high = [e for e in high_impact if -NEWS_LOOKBACK_HOURS * 60 <= e["mins_diff"] <= -NEWS_COOLDOWN_MIN]
    if post_high:
        ev = max(post_high, key=lambda e: e["dt"])
        first_full_hour = math.ceil(ev["dt"].timestamp() / 3600) * 3600
        has_closed_hour = any(int(ts) >= first_full_hour for ts in (hourly_times or []))

        if not has_closed_hour:
            return {
                "ok": True,
                "state": "wait_hour",
                "events": events,
                "evaluated_lines": evaluated_lines,
                "explanation": f"Post-News '{ev['name']}': attendo la chiusura della prima candela oraria completa. Max 2/10.",
                "penalty": 0,
                "max_score_cap": 2,
            }

        try:
            reaction = get_post_news_reaction(ev)
        except Exception:
            reaction = None

        if reaction is None:
            return {
                "ok": True,
                "state": "reaction_unknown",
                "events": events,
                "evaluated_lines": evaluated_lines,
                "explanation": f"Impossibile verificare la reazione a 5m di SOL dopo '{ev['name']}'. Max 2/10.",
                "penalty": 0,
                "max_score_cap": 2,
            }

        reac_text = (
            f"Reazione SOL a '{ev['name']}': {reaction['direction']} ({reaction['reaction_pct']:+.2f}%), "
            f"range ultimi 15m del {reaction['range_15m_pct']:.2f}%."
        )
        if not reaction["stable"]:
            return {
                "ok": True,
                "state": "unstable",
                "events": events,
                "evaluated_lines": evaluated_lines,
                "explanation": f"{reac_text} ⚠️ Mercato ancora troppo nervoso post-news. Punteggio bloccato a max 3/10.",
                "penalty": 0,
                "max_score_cap": 3,
            }
        else:
            # Post-news stabile, controlliamo se ci sono news di medio impatto imminenti
            upcoming_med = [e for e in med_impact if -15 <= e["mins_diff"] <= 60]
            if upcoming_med:
                return {
                    "ok": True,
                    "state": "post_clear_med_near",
                    "events": events,
                    "evaluated_lines": evaluated_lines,
                    "explanation": f"{reac_text} ✅ Assorbita bene, ma c'è una news di medio impatto vicina (-1 pt).",
                    "penalty": 1,
                    "max_score_cap": 10,
                }
            return {
                "ok": True,
                "state": "post_clear",
                "events": events,
                "evaluated_lines": evaluated_lines,
                "explanation": f"{reac_text} ✅ Volatilità post-news rientrata con successo. Nessuna penalità.",
                "penalty": 0,
                "max_score_cap": 10,
            }

    # 4. Nessuna High News, controlliamo le Medium News vicine (-15m a +60m)
    upcoming_med = [e for e in med_impact if -15 <= e["mins_diff"] <= 60]
    if upcoming_med:
        ev = upcoming_med[0]
        return {
            "ok": True,
            "state": "med_near",
            "events": events,
            "evaluated_lines": evaluated_lines,
            "explanation": f"Notizia a medio impatto vicina ('{ev['name']}'). Applicata leggera cautela (-1 pt).",
            "penalty": 1,
            "max_score_cap": 9,
        }

    return {
        "ok": True,
        "state": "clear",
        "events": events,
        "evaluated_lines": evaluated_lines,
        "explanation": "✅ Via libera macroeconomico: nessuna notizia pericolosa rilevata nel breve termine (0 penalità).",
        "penalty": 0,
        "max_score_cap": 10,
    }


# ---------------------------------------------------------------------
# COSTRUZIONE VISUAL REPORT E CALCOLO FINALE
# ---------------------------------------------------------------------

def make_progress_bar(score):
    if score >= 8:
        fill = "🟩"
    elif score >= 5:
        fill = "🟨"
    else:
        fill = "🟥"
    return fill * score + "⬜" * (10 - score)


def check_active_bot(price):
    if not (ACTIVE_LOWER and ACTIVE_UPPER):
        return None
    try:
        low_b = float(ACTIVE_LOWER)
        up_b = float(ACTIVE_UPPER)
        if low_b >= up_b:
            return None
        if price < low_b or price > up_b:
            return f"🚨 ATTENZIONE: Il tuo Bot attivo ({low_b:.2f} - {up_b:.2f}) è FUORI RANGE!"
        edge_pct = min(price - low_b, up_b - price) / price * 100
        if edge_pct < 1.5:
            return f"⚠️ NOTA: Bot attivo ({low_b:.2f} - {up_b:.2f}) vicino al bordo ({edge_pct:.1f}%)."
        return f"✅ BOT GIÀ ATTIVO NEL RANGE ({low_b:.2f} - {up_b:.2f}). Non aprirne un altro."
    except ValueError:
        return None


def build_report(analysis, news):
    tech_score = analysis["score"]
    penalty = news["penalty"]
    cap = news["max_score_cap"]

    # Calcolo Punteggio Finale (1-10)
    final_score = max(1, min(10, min(tech_score - penalty, cap)))
    action_info = SCORE_ACTIONS[final_score]
    bar = make_progress_bar(final_score)
    now_str = datetime.now(LOCAL_TZ).strftime("%d/%m %H:%M")

    lines = [
        f"🤖 ANALISI SOL/USDT — {now_str}",
        f"💰 Prezzo: {analysis['price']:.2f}$ (24h: {analysis['chg24']:+.1f}% | 7d: {analysis['chg7d']:+.1f}%)",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📊 PUNTEGGIO FINALE: {final_score}/10",
        f"{bar}",
        "",
        f"{action_info['title']}",
        f"💡 {action_info['desc']}",
    ]

    # Stato bot già attivo (se configurato)
    active_msg = check_active_bot(analysis["price"])
    if active_msg:
        lines.extend(["", active_msg])

    # Mostra parametri Pionex (consigliati da 7 in su, oppure indicativi sotto 7)
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    if final_score >= 7:
        lines.append("🛠️ PARAMETRI PIONEX CONSIGLIATI:")
    else:
        lines.append("🛠️ PARAMETRI IPOTETICI (Apertura Sconsigliata ora):")

    lines.extend([
        f"• Range: {analysis['lower']:.2f}$ — {analysis['upper']:.2f}$",
        f"• Griglie: {analysis['grids']} (Step: ~{analysis['step_pct']:.2f}%)",
        f"• Stop Loss: {analysis['stop']:.2f}$",
        f"• Budget stimato: ~{analysis['invest_usdt']:.0f} USDT",
    ])

    # Spiegazione Punteggio Tecnico
    lines.extend([
        "━━━━━━━━━━━━━━━━━━━━",
        f"🧠 PERCHÉ QUESTO PUNTEGGIO? (Tecnica: {tech_score}/10)",
    ])
    lines.extend(analysis["reasons"])

    # Spiegazione Notizie Valutate
    lines.extend([
        "━━━━━━━━━━━━━━━━━━━━",
        f"📰 NOTIZIE VALUTATE ({', '.join( sorted(NEWS_COUNTRIES) ).upper()}):",
    ])
    lines.extend(news["evaluated_lines"])
    lines.extend([
        "",
        f"⚖️ Impatto News sul voto: {news['explanation']}"
    ])

    return "\n".join(lines)


# ---------------------------------------------------------------------
# AVVIO
# ---------------------------------------------------------------------

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
        print("\nTelegram non configurato: imposta TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID.")
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
