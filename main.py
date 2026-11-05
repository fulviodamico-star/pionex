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

# Feed pubblico del calendario economico; non richiede una API key.
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Il feed usa normalmente codici valuta, per esempio USD, EUR, GBP.
# Puoi impostare più valute, per esempio NEWS_COUNTRIES=USD,EUR.
COUNTRY_ALIASES = {
    "us": "usd",
    "usa": "usd",
    "united states": "usd",
    "united states of america": "usd",
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

NEWS_COUNTRIES_LABEL = ", ".join(
    country.upper() for country in sorted(NEWS_COUNTRIES)
)

LOCAL_TZ_NAME = os.getenv("LOCAL_TZ", "Europe/Rome").strip()

try:
    LOCAL_TZ = ZoneInfo(LOCAL_TZ_NAME)
except Exception:
    print(f"⚠️ Fuso orario {LOCAL_TZ_NAME!r} non valido; uso UTC.")
    LOCAL_TZ = timezone.utc
    LOCAL_TZ_NAME = "UTC"

# Filtro news.
NEWS_BLOCK_BEFORE_MIN = int(os.getenv("NEWS_BLOCK_BEFORE_MIN", "120"))
NEWS_COOLDOWN_MIN = int(os.getenv("NEWS_COOLDOWN_MIN", "45"))
NEWS_LOOKBACK_HOURS = int(os.getenv("NEWS_LOOKBACK_HOURS", "4"))

# Per le news di impatto medio usiamo una finestra più corta.
NEWS_MEDIUM_BLOCK_BEFORE_MIN = int(
    os.getenv("NEWS_MEDIUM_BLOCK_BEFORE_MIN", "60")
)
NEWS_MEDIUM_COOLDOWN_MIN = int(
    os.getenv("NEWS_MEDIUM_COOLDOWN_MIN", "30")
)

# Soglie per classificare come stabile la reazione recente di SOL.
# Sono soglie indicative da verificare e calibrare.
NEWS_MAX_15M_RANGE_PCT = float(
    os.getenv("NEWS_MAX_15M_RANGE_PCT", "1.0")
)
NEWS_MAX_15M_MOVE_PCT = float(
    os.getenv("NEWS_MAX_15M_MOVE_PCT", "0.8")
)

# Impostazioni della griglia.
INVEST_EUR = 50
MIN_ORDER_USDT = 5.0
TARGET_STEP_PCT = 0.30
MIN_STEP_PCT = 0.25

MAX_NEWS_EVENTS_IN_REPORT = 10


# ---------------------------------------------------------------------
# SCALA OPERATIVA
# ---------------------------------------------------------------------

SCORE_GUIDE = {
    1: (
        "NON APRIRE NULLA",
        "condizioni molto sfavorevoli",
    ),
    2: (
        "NON APRIRE",
        "rischio molto alto",
    ),
    3: (
        "NON APRIRE",
        "vantaggio insufficiente o rischio news elevato",
    ),
    4: (
        "ATTENDI",
        "segnali deboli",
    ),
    5: (
        "ATTENDI",
        "quadro misto, senza un vantaggio chiaro",
    ),
    6: (
        "ATTENDI UNA CONFERMA",
        "segnali discreti ma non sufficienti per aprire",
    ),
    7: (
        "VALUTA APERTURA PRUDENTE",
        "setup accettabile; controlla parametri e minimo d'ordine",
    ),
    8: (
        "APRI CON CAUTELA",
        "setup favorevole; usa i parametri proposti",
    ),
    9: (
        "APRI ORA",
        "setup molto favorevole; usa i parametri proposti",
    ),
    10: (
        "APRI SUBITO IL BOT",
        "massima valutazione; usa i parametri proposti",
    ),
}


# ---------------------------------------------------------------------
# DATI DI MERCATO
# ---------------------------------------------------------------------

def get_candles(interval=60):
    """
    Scarica candele SOL/USD da Kraken.
    Scarta l'ultima candela, che potrebbe essere ancora aperta.
    """
    response = requests.get(
        "https://api.kraken.com/0/public/OHLC",
        params={
            "pair": "SOLUSD",
            "interval": interval,
        },
        timeout=15,
    )
    response.raise_for_status()

    data = response.json()

    if data.get("error"):
        raise RuntimeError(f"Kraken: {data['error']}")

    result = data.get("result", {})
    pair_key = next(
        (name for name in result if name != "last"),
        None,
    )

    if pair_key is None:
        raise RuntimeError("Kraken non ha restituito candele.")

    rows = result[pair_key][:-1]

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
    if not values:
        raise ValueError("Lista vuota per il calcolo EMA.")

    multiplier = 2 / (n + 1)
    result = values[0]

    for value in values[1:]:
        result = value * multiplier + result * (1 - multiplier)

    return result


def rsi(closes, n=14):
    if len(closes) <= n:
        raise ValueError("Dati insufficienti per il calcolo RSI.")

    gains = []
    losses = []

    for i in range(1, len(closes)):
        difference = closes[i] - closes[i - 1]
        gains.append(max(difference, 0))
        losses.append(max(-difference, 0))

    average_gain = sum(gains[:n]) / n
    average_loss = sum(losses[:n]) / n

    for gain, loss in zip(gains[n:], losses[n:]):
        average_gain = (average_gain * (n - 1) + gain) / n
        average_loss = (average_loss * (n - 1) + loss) / n

    if average_loss == 0:
        return 100.0

    return 100 - 100 / (1 + average_gain / average_loss)


def atr_adx(high, low, close, n=14):
    if len(close) <= n:
        raise ValueError("Dati insufficienti per ATR/ADX.")

    true_ranges = []
    positive_dm = []
    negative_dm = []

    for i in range(1, len(close)):
        upward_move = high[i] - high[i - 1]
        downward_move = low[i - 1] - low[i]

        positive_dm.append(
            upward_move
            if upward_move > downward_move and upward_move > 0
            else 0
        )
        negative_dm.append(
            downward_move
            if downward_move > upward_move and downward_move > 0
            else 0
        )

        true_ranges.append(
            max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i] - close[i - 1]),
            )
        )

    smoothed_tr = sum(true_ranges[:n])
    smoothed_positive = sum(positive_dm[:n])
    smoothed_negative = sum(negative_dm[:n])
    dx_values = []

    for i in range(n, len(true_ranges) + 1):
        if i > n:
            smoothed_tr = (
                smoothed_tr
                - smoothed_tr / n
                + true_ranges[i - 1]
            )
            smoothed_positive = (
                smoothed_positive
                - smoothed_positive / n
                + positive_dm[i - 1]
            )
            smoothed_negative = (
                smoothed_negative
                - smoothed_negative / n
                + negative_dm[i - 1]
            )

        if smoothed_tr <= 0:
            dx_values.append(0.0)
            continue

        pdi = 100 * smoothed_positive / smoothed_tr
        mdi = 100 * smoothed_negative / smoothed_tr

        if pdi + mdi == 0:
            dx_values.append(0.0)
        else:
            dx_values.append(
                100 * abs(pdi - mdi) / (pdi + mdi)
            )

    if not dx_values:
        return 0.0, 0.0

    seed = dx_values[:n]
    adx_value = sum(seed) / len(seed)

    for dx in dx_values[n:]:
        adx_value = (adx_value * (n - 1) + dx) / n

    return smoothed_tr / n, adx_value


def percentile(values, p):
    if not values:
        raise ValueError("Lista vuota per il calcolo del percentile.")

    ordered = sorted(values)
    index = min(
        len(ordered) - 1,
        max(0, int(round((len(ordered) - 1) * p))),
    )
    return ordered[index]


# ---------------------------------------------------------------------
# ANALISI TECNICA E PUNTEGGIO BASE
# ---------------------------------------------------------------------

def analyze():
    candles = get_candles(interval=60)
    high = candles["high"]
    low = candles["low"]
    close = candles["close"]

    if len(close) < 200:
        raise RuntimeError(
            f"Candele orarie insufficienti: ne servono 200, "
            f"ricevute {len(close)}."
        )

    price = close[-1]

    if price <= 0:
        raise RuntimeError("Prezzo SOL non valido.")

    ema20 = ema(close[-200:], 20)
    ema50 = ema(close[-200:], 50)
    rsi14 = rsi(close[-200:])
    atr, adx = atr_adx(
        high[-200:],
        low[-200:],
        close[-200:],
    )

    atr_pct = atr / price * 100
    change24 = (price / close[-25] - 1) * 100
    change7d = (price / close[-169] - 1) * 100

    high24 = max(high[-24:])
    low24 = min(low[-24:])
    high7d = max(high[-168:])
    low7d = min(low[-168:])

    # Escursione media giornaliera degli ultimi 7 giorni.
    daily_ranges = []

    for day in range(7):
        segment_high = high[-(day + 1) * 24: len(high) - day * 24]
        segment_low = low[-(day + 1) * 24: len(low) - day * 24]

        daily_ranges.append(
            (max(segment_high) - min(segment_low)) / price * 100
        )

    average_daily = sum(daily_ranges) / len(daily_ranges)

    # Range proposto: percentili degli ultimi 7 giorni,
    # con limiti di larghezza rispetto al prezzo.
    lower = percentile(low[-168:], 0.10)
    upper = percentile(high[-168:], 0.90)

    lower = min(max(lower, price * 0.92), price * 0.965)
    upper = max(min(upper, price * 1.08), price * 1.035)

    width_pct = (upper - lower) / price * 100
    invest_usdt = INVEST_EUR * get_eurusd()

    max_grids_by_money = max(
        1,
        int(invest_usdt // MIN_ORDER_USDT),
    )

    target_grids = max(
        5,
        min(50, round(width_pct / TARGET_STEP_PCT)),
    )

    grids = min(target_grids, max_grids_by_money)
    step_pct = width_pct / grids
    stop_loss = lower * 0.975
    position_in_range = (price - lower) / (upper - lower) * 100

    # Ogni indicatore contribuisce al punteggio.
    contributions = []
    score_reasons = []

    def add_reason(label, points, explanation):
        contributions.append(points)
        score_reasons.append(
            f"{label}: {points:+d} — {explanation}"
        )

    # ADX: preferenza per un mercato senza trend forte.
    if adx < 20:
        add_reason("ADX", 2, f"{adx:.0f}, mercato laterale")
    elif adx < 25:
        add_reason("ADX", 1, f"{adx:.0f}, trend debole")
    elif adx < 30:
        add_reason("ADX", 0, f"{adx:.0f}, trend moderato")
    else:
        add_reason("ADX", -2, f"{adx:.0f}, trend forte")

    # Distanza tra EMA20 ed EMA50.
    ema_gap_pct = abs(ema20 - ema50) / price * 100

    if ema_gap_pct < 1:
        add_reason(
            "Distanza EMA20/EMA50",
            2,
            f"{ema_gap_pct:.1f}%, EMA vicine",
        )
    elif ema_gap_pct < 2:
        add_reason(
            "Distanza EMA20/EMA50",
            1,
            f"{ema_gap_pct:.1f}%, distanza contenuta",
        )
    elif ema_gap_pct < 3:
        add_reason(
            "Distanza EMA20/EMA50",
            0,
            f"{ema_gap_pct:.1f}%, tendenza moderata",
        )
    else:
        add_reason(
            "Distanza EMA20/EMA50",
            -2,
            f"{ema_gap_pct:.1f}%, tendenza marcata",
        )

    # RSI: preferenza per valori neutri.
    if 40 <= rsi14 <= 60:
        add_reason("RSI", 2, f"{rsi14:.0f}, zona neutra")
    elif 35 <= rsi14 <= 65:
        add_reason("RSI", 1, f"{rsi14:.0f}, leggermente sbilanciato")
    elif 30 <= rsi14 <= 70:
        add_reason("RSI", 0, f"{rsi14:.0f}, entro la fascia normale")
    else:
        add_reason(
            "RSI",
            -2,
            f"{rsi14:.0f}, zona estrema",
        )

    # Posizione del prezzo all'interno del range proposto.
    if 40 <= position_in_range <= 60:
        add_reason(
            "Posizione nel range",
            2,
            f"{position_in_range:.0f}%, zona centrale",
        )
    elif 30 <= position_in_range <= 70:
        add_reason(
            "Posizione nel range",
            1,
            f"{position_in_range:.0f}%, zona abbastanza centrale",
        )
    elif 20 <= position_in_range <= 80:
        add_reason(
            "Posizione nel range",
            0,
            f"{position_in_range:.0f}%, zona non centrale",
        )
    else:
        add_reason(
            "Posizione nel range",
            -2,
            f"{position_in_range:.0f}%, troppo vicina a un estremo",
        )

    # Movimento giornaliero: serve movimento, ma eccessi indicano volatilità.
    if 2 <= average_daily <= 6:
        add_reason(
            "Escursione media giornaliera",
            2,
            f"{average_daily:.1f}%, movimento adeguato",
        )
    elif (
        1.5 <= average_daily < 2
        or 6 < average_daily <= 8
    ):
        add_reason(
            "Escursione media giornaliera",
            1,
            f"{average_daily:.1f}%, accettabile ma non ideale",
        )
    elif average_daily < 1.5:
        add_reason(
            "Escursione media giornaliera",
            -1,
            f"{average_daily:.1f}%, movimento scarso",
        )
    else:
        add_reason(
            "Escursione media giornaliera",
            -2,
            f"{average_daily:.1f}%, volatilità elevata",
        )

    # Penalità per variazioni giornaliere brusche.
    if abs(change24) > 8:
        add_reason(
            "Variazione 24h",
            -2,
            f"{change24:+.1f}%, movimento molto brusco",
        )
    elif abs(change24) > 5:
        add_reason(
            "Variazione 24h",
            -1,
            f"{change24:+.1f}%, movimento elevato",
        )
    else:
        add_reason(
            "Variazione 24h",
            0,
            f"{change24:+.1f}%, nessuna penalità",
        )

    # Penalità se il passo è troppo stretto rispetto alle commissioni.
    if step_pct < MIN_STEP_PCT:
        add_reason(
            "Passo griglia",
            -1,
            f"{step_pct:.2f}%, potenzialmente eroso dalle commissioni",
        )
    else:
        add_reason(
            "Passo griglia",
            0,
            f"{step_pct:.2f}%, sopra la soglia minima impostata",
        )

    raw_score = sum(contributions)
    technical_score = max(1, min(10, raw_score))

    if raw_score < 1:
        score_clamp_note = "La somma tecnica è stata limitata al minimo di 1."
    elif raw_score > 10:
        score_clamp_note = "La somma tecnica è stata limitata al massimo di 10."
    else:
        score_clamp_note = ""

    return {
        "price": price,
        "ema20": ema20,
        "ema50": ema50,
        "ema_gap_pct": ema_gap_pct,
        "rsi": rsi14,
        "adx": adx,
        "atr_pct": atr_pct,
        "change24": change24,
        "change7d": change7d,
        "high24": high24,
        "low24": low24,
        "high7d": high7d,
        "low7d": low7d,
        "average_daily": average_daily,
        "lower": lower,
        "upper": upper,
        "grids": grids,
        "step_pct": step_pct,
        "stop_loss": stop_loss,
        "position_in_range": position_in_range,
        "technical_score": technical_score,
        "raw_score": raw_score,
        "score_clamp_note": score_clamp_note,
        "score_reasons": score_reasons,
        "invest_usdt": invest_usdt,
        "per_grid": invest_usdt / grids,
        "candle_times": candles["time"],
    }


# ---------------------------------------------------------------------
# CALENDARIO ECONOMICO PUBBLICO
# ---------------------------------------------------------------------

def parse_calendar_time(value):
    """Converte l'orario ISO del feed in UTC."""
    raw = str(value or "").strip()

    if not raw:
        raise ValueError("Orario evento mancante.")

    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"

    event_time = datetime.fromisoformat(raw)

    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)

    return event_time.astimezone(timezone.utc)


def normalize_impact(value):
    raw = str(value or "").strip().lower()

    if "high" in raw:
        return "high"
    if "medium" in raw or "moderate" in raw:
        return "medium"
    if "low" in raw:
        return "low"
    if "holiday" in raw:
        return "holiday"

    return raw or "unknown"


def clean_text(value, max_length=80):
    text = " ".join(str(value or "").split())
    if len(text) > max_length:
        text = text[:max_length - 1].rstrip() + "…"
    return text


def value_is_present(value):
    if value is None:
        return False

    text = str(value).strip().lower()
    return text not in ("", "none", "nan", "null")


def fetch_macro_events(now):
    """
    Scarica gli eventi macro della settimana corrente.
    Il feed pubblico non richiede una chiave API.
    """
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; GridBot/1.0)",
    }

    try:
        response = requests.get(
            CALENDAR_URL,
            headers=headers,
            timeout=20,
        )
    except requests.Timeout:
        raise RuntimeError("Timeout del feed calendario.") from None
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Errore di rete del calendario ({type(exc).__name__})."
        ) from None

    if response.status_code != 200:
        raise RuntimeError(
            f"Feed calendario HTTP {response.status_code}."
        )

    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError(
            "Il feed calendario non ha restituito JSON valido."
        ) from None

    if not isinstance(payload, list):
        raise RuntimeError(
            "Formato del feed calendario non riconosciuto."
        )

    today_local = now.astimezone(LOCAL_TZ).date()
    events = []

    # Conserviamo tutti gli eventi di oggi e quelli recenti/imminenti
    # che possono influenzare il filtro operativo.
    upcoming_window = max(
        NEWS_BLOCK_BEFORE_MIN,
        NEWS_MEDIUM_BLOCK_BEFORE_MIN,
    )

    for item in payload:
        if not isinstance(item, dict):
            continue

        country = str(item.get("country") or "").strip()

        if normalize_country(country) not in NEWS_COUNTRIES:
            continue

        date_value = item.get("date")
        if not date_value:
            continue

        try:
            event_time = parse_calendar_time(date_value)
        except (TypeError, ValueError):
            continue

        local_time = event_time.astimezone(LOCAL_TZ)
        minutes_to_event = (event_time - now).total_seconds() / 60

        is_today = local_time.date() == today_local
        relevant_for_filter = (
            -NEWS_LOOKBACK_HOURS * 60
            <= minutes_to_event
            <= upcoming_window
        )

        if not is_today and not relevant_for_filter:
            continue

        events.append(
            {
                "dt": event_time,
                "local_dt": local_time,
                "country": country.upper(),
                "impact": normalize_impact(item.get("impact")),
                "name": clean_text(
                    item.get("title")
                    or item.get("event")
                    or "Evento",
                    100,
                ),
                "actual": item.get("actual"),
                "forecast": item.get("forecast") or item.get("estimate"),
                "previous": item.get("previous") or item.get("prev"),
                "filter_effect": "nessun limite attivo",
            }
        )

    events.sort(key=lambda event: event["dt"])
    print(f"Calendario caricato: {len(events)} eventi pertinenti.")

    return events


def get_post_news_reaction(event):
    """
    Misura la reazione di SOL dopo la news usando candele da 5 minuti.
    Non interpreta il titolo della notizia come positivo o negativo.
    """
    candles = get_candles(interval=5)
    times = candles["time"]
    closes = candles["close"]

    if len(closes) < 4:
        return None

    interval_seconds = 5 * 60
    release_timestamp = event["dt"].timestamp()

    first_post_timestamp = (
        math.ceil(release_timestamp / interval_seconds)
        * interval_seconds
    )

    post_indices = [
        i for i, timestamp in enumerate(times)
        if timestamp >= first_post_timestamp
    ]

    pre_indices = [
        i for i, timestamp in enumerate(times)
        if timestamp + interval_seconds <= release_timestamp
    ]

    if len(pre_indices) < 1 or len(post_indices) < 4:
        return None

    last_four = post_indices[-4:]

    # Non usare una misura che contiene candele mancanti.
    if any(
        times[b] - times[a] != interval_seconds
        for a, b in zip(last_four, last_four[1:])
    ):
        return None

    latest_bar_close_time = times[-1] + interval_seconds
    now_timestamp = datetime.now(timezone.utc).timestamp()

    if now_timestamp - latest_bar_close_time > 15 * 60:
        return None

    price_before = closes[pre_indices[-1]]
    current_price = closes[last_four[-1]]

    if price_before <= 0 or current_price <= 0:
        return None

    reaction_pct = (current_price / price_before - 1) * 100

    move_15m_pct = (
        closes[last_four[-1]] / closes[last_four[0]] - 1
    ) * 100

    last_three = last_four[-3:]
    high_15m = max(candles["high"][i] for i in last_three)
    low_15m = min(candles["low"][i] for i in last_three)
    range_15m_pct = (high_15m - low_15m) / current_price * 100

    stable = (
        abs(move_15m_pct) <= NEWS_MAX_15M_MOVE_PCT
        and range_15m_pct <= NEWS_MAX_15M_RANGE_PCT
    )

    if reaction_pct > 0.30:
        direction = "rialzista"
    elif reaction_pct < -0.30:
        direction = "ribassista"
    else:
        direction = "neutra"

    return {
        "reaction_pct": reaction_pct,
        "move_15m_pct": move_15m_pct,
        "range_15m_pct": range_15m_pct,
        "stable": stable,
        "direction": direction,
    }


def get_news_context(hourly_times):
    now = datetime.now(timezone.utc)

    try:
        events = fetch_macro_events(now)
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            error = str(exc)
        else:
            error = f"Errore calendario ({type(exc).__name__})."

        print(f"Calendario non disponibile: {error}")

        return {
            "ok": False,
            "events": [],
            "cap": 3,
            "gate_reasons": [
                "Calendario non verificato: punteggio massimo 3/10."
            ],
            "reaction": None,
            "now": now,
            "error": error,
        }

    limits = []
    notes = []
    reaction = None

    def add_limit(event, cap, reason, event_effect):
        limits.append(
            {
                "cap": cap,
                "reason": reason,
            }
        )
        if event is not None:
            event["filter_effect"] = event_effect

    high_impact = [
        event for event in events
        if event["impact"] == "high"
    ]

    medium_impact = [
        event for event in events
        if event["impact"] == "medium"
    ]

    # News HIGH in arrivo: limita il punteggio a 3/10.
    high_upcoming = [
        event for event in high_impact
        if 0 <= (event["dt"] - now).total_seconds() / 60
        <= NEWS_BLOCK_BEFORE_MIN
    ]

    for event in high_upcoming:
        event["filter_effect"] = "news HIGH: massimo 3/10"

    if high_upcoming:
        nearest = min(high_upcoming, key=lambda event: event["dt"])
        minutes = max(
            0,
            math.ceil((nearest["dt"] - now).total_seconds() / 60),
        )
        add_limit(
            nearest,
            3,
            f"News HIGH {nearest['name']} tra {minutes} min: "
            "punteggio massimo 3/10.",
            "news HIGH: massimo 3/10",
        )

    # News HIGH appena pubblicata: attendi il cooldown.
    high_recent = [
        event for event in high_impact
        if 0 <= (now - event["dt"]).total_seconds() / 60
        < NEWS_COOLDOWN_MIN
    ]

    for event in high_recent:
        event["filter_effect"] = "news HIGH appena uscita: massimo 2/10"

    if high_recent:
        latest = max(high_recent, key=lambda event: event["dt"])
        elapsed = max(
            0,
            math.floor((now - latest["dt"]).total_seconds() / 60),
        )
        add_limit(
            latest,
            2,
            f"News HIGH {latest['name']} uscita da {elapsed} min: "
            "cooldown attivo, massimo 2/10.",
            "news HIGH appena uscita: massimo 2/10",
        )

    # News HIGH passata: aspetta una candela oraria e controlla la reazione SOL.
    high_post = [
        event for event in high_impact
        if NEWS_COOLDOWN_MIN
        <= (now - event["dt"]).total_seconds() / 60
        <= NEWS_LOOKBACK_HOURS * 60
    ]

    if high_post:
        latest_post = max(high_post, key=lambda event: event["dt"])

        first_full_hour = (
            math.ceil(latest_post["dt"].timestamp() / 3600) * 3600
        )

        has_closed_post_news_hour = any(
            int(timestamp) >= first_full_hour
            for timestamp in (hourly_times or [])
        )

        if not has_closed_post_news_hour:
            reason = (
                f"News HIGH {latest_post['name']}: manca una candela "
                "oraria completa, massimo 3/10."
            )
            add_limit(
                latest_post,
                3,
                reason,
                "attesa candela oraria: massimo 3/10",
            )

        else:
            try:
                reaction = get_post_news_reaction(latest_post)
            except Exception as exc:
                print(
                    "Reazione post-news non disponibile "
                    f"({type(exc).__name__})."
                )
                reaction = None

            if reaction is None:
                reason = (
                    f"News HIGH {latest_post['name']}: reazione SOL non "
                    "verificata, massimo 3/10."
                )
                add_limit(
                    latest_post,
                    3,
                    reason,
                    "reazione non verificata: massimo 3/10",
                )

            elif not reaction["stable"]:
                reason = (
                    f"News HIGH {latest_post['name']}: SOL è ancora "
                    f"volatile (movimento 15m "
                    f"{reaction['move_15m_pct']:+.2f}%, range "
                    f"{reaction['range_15m_pct']:.2f}%); massimo 3/10."
                )
                add_limit(
                    latest_post,
                    3,
                    reason,
                    "SOL ancora volatile: massimo 3/10",
                )

            else:
                latest_post["filter_effect"] = (
                    "reazione SOL stabile: nessun taglio"
                )
                notes.append(
                    f"News HIGH {latest_post['name']}: cooldown superato "
                    "e reazione SOL stabile; nessun taglio news."
                )

    # News MEDIUM imminente: il punteggio non può superare 6/10.
    medium_upcoming = [
        event for event in medium_impact
        if 0 <= (event["dt"] - now).total_seconds() / 60
        <= NEWS_MEDIUM_BLOCK_BEFORE_MIN
    ]

    for event in medium_upcoming:
        event["filter_effect"] = "news MEDIUM: massimo 6/10"

    if medium_upcoming:
        nearest = min(medium_upcoming, key=lambda event: event["dt"])
        minutes = max(
            0,
            math.ceil((nearest["dt"] - now).total_seconds() / 60),
        )
        add_limit(
            nearest,
            6,
            f"News MEDIUM {nearest['name']} tra {minutes} min: "
            "punteggio massimo 6/10."
            ,
            "news MEDIUM: massimo 6/10",
        )

    # News MEDIUM appena pubblicata: breve attesa.
    medium_recent = [
        event for event in medium_impact
        if 0 <= (now - event["dt"]).total_seconds() / 60
        < NEWS_MEDIUM_COOLDOWN_MIN
    ]

    for event in medium_recent:
        event["filter_effect"] = "news MEDIUM appena uscita: massimo 5/10"

    if medium_recent:
        latest = max(medium_recent, key=lambda event: event["dt"])
        elapsed = max(
            0,
            math.floor((now - latest["dt"]).total_seconds() / 60),
        )
        add_limit(
            latest,
            5,
            f"News MEDIUM {latest['name']} uscita da {elapsed} min: "
            "attesa prudenziale, massimo 5/10.",
            "news MEDIUM appena uscita: massimo 5/10",
        )

    cap = min(
        (item["cap"] for item in limits),
        default=10,
    )

    gate_reasons = []

    for item in limits:
        if item["reason"] not in gate_reasons:
            gate_reasons.append(item["reason"])

    for note in notes:
        if note not in gate_reasons:
            gate_reasons.append(note)

    if not gate_reasons:
        gate_reasons.append(
            f"Nessuna news HIGH nei prossimi {NEWS_BLOCK_BEFORE_MIN} min "
            f"e nessuna MEDIUM nei prossimi "
            f"{NEWS_MEDIUM_BLOCK_BEFORE_MIN} min: nessun taglio news."
        )

    return {
        "ok": True,
        "events": events,
        "cap": cap,
        "gate_reasons": gate_reasons,
        "reaction": reaction,
        "now": now,
        "error": None,
    }


# ---------------------------------------------------------------------
# STATO EVENTUALE BOT GIÀ ATTIVO
# ---------------------------------------------------------------------

def active_bot_status(analysis):
    lower_set = bool(ACTIVE_LOWER)
    upper_set = bool(ACTIVE_UPPER)

    if not lower_set and not upper_set:
        return None

    if lower_set != upper_set:
        return (
            "Range attivo incompleto: controlla ACTIVE_LOWER e "
            "ACTIVE_UPPER prima di aprire un nuovo bot."
        )

    try:
        active_lower = float(ACTIVE_LOWER)
        active_upper = float(ACTIVE_UPPER)

        if active_lower >= active_upper:
            return (
                "Range attivo non valido: controlla ACTIVE_LOWER e "
                "ACTIVE_UPPER prima di aprire un nuovo bot."
            )

        price = analysis["price"]

        if price < active_lower or price > active_upper:
            return (
                f"Bot già attivo fuori range: prezzo ${price:.2f}, "
                f"range {active_lower:.2f}–{active_upper:.2f}. "
                "Controlla il bot esistente; non aprirne un secondo."
            )

        edge_pct = min(
            price - active_lower,
            active_upper - price,
        ) / price * 100

        if edge_pct < 1.5:
            return (
                f"Bot già attivo vicino al bordo "
                f"({edge_pct:.1f}%). Controllalo su Pionex; "
                "non aprire un secondo bot."
            )

        return (
            f"Bot già attivo nel range "
            f"{active_lower:.2f}–{active_upper:.2f}; "
            "non aprire un secondo bot."
        )

    except (ValueError, TypeError):
        return (
            "Range attivo non valido: controlla ACTIVE_LOWER e "
            "ACTIVE_UPPER."
        )


# ---------------------------------------------------------------------
# REPORT
# ---------------------------------------------------------------------

def format_time_distance(event, now):
    delta_minutes = (event["dt"] - now).total_seconds() / 60

    if abs(delta_minutes) < 1:
        return "adesso"

    minutes = math.ceil(abs(delta_minutes))

    if minutes < 60:
        duration = f"{minutes} min"
    else:
        hours = minutes // 60
        remaining_minutes = minutes % 60

        if remaining_minutes:
            duration = f"{hours}h {remaining_minutes}m"
        else:
            duration = f"{hours}h"

    return f"tra {duration}" if delta_minutes > 0 else f"{duration} fa"


def format_news_lines(news, max_events=MAX_NEWS_EVENTS_IN_REPORT):
    if not news or not news.get("ok"):
        error = (
            news.get("error", "errore non specificato")
            if news
            else "calendario non disponibile"
        )

        return [
            f"• Calendario non disponibile: {clean_text(error, 160)}",
            "• Filtro prudenziale applicato: punteggio massimo 3/10.",
        ]

    lines = []

    events = sorted(
        news.get("events", []),
        key=lambda event: event["dt"],
    )

    if not events:
        lines.append(
            f"• Nessun evento {NEWS_COUNTRIES_LABEL} oggi o nella "
            "finestra operativa."
        )
    else:
        today_local = news["now"].astimezone(LOCAL_TZ).date()

        for event in events[:max_events]:
            if event["local_dt"].date() == today_local:
                time_text = event["local_dt"].strftime("%H:%M")
            else:
                time_text = event["local_dt"].strftime("%d/%m %H:%M")

            impact = event["impact"].upper()
            title = clean_text(event["name"], 62)
            distance = format_time_distance(event, news["now"])

            details = []

            if value_is_present(event.get("forecast")):
                details.append(
                    "stima "
                    + clean_text(event["forecast"], 18)
                )

            if value_is_present(event.get("actual")):
                details.append(
                    "effettivo "
                    + clean_text(event["actual"], 18)
                )

            if value_is_present(event.get("previous")):
                details.append(
                    "prec. "
                    + clean_text(event["previous"], 18)
                )

            line = (
                f"• {time_text} {event['country']} {impact} — {title} "
                f"| {distance}"
            )

            if details:
                line += " | " + ", ".join(details)

            effect = clean_text(
                event.get("filter_effect", "nessun limite attivo"),
                48,
            )
            line += f" | filtro: {effect}"

            lines.append(line)

        if len(events) > max_events:
            lines.append(
                f"• Altri {len(events) - max_events} eventi "
                "non mostrati."
            )

    for reason in news.get("gate_reasons", [])[:4]:
        lines.append(f"• Filtro calendario: {clean_text(reason, 190)}")

    reaction = news.get("reaction")

    if reaction:
        lines.append(
            f"• Reazione SOL post-news: {reaction['direction']}, "
            f"{reaction['reaction_pct']:+.2f}% dall'evento; "
            f"ultimi 15m {reaction['move_15m_pct']:+.2f}%, "
            f"range {reaction['range_15m_pct']:.2f}%."
        )

    return lines


def build_report(analysis, news):
    technical_score = analysis["technical_score"]

    if news and news.get("ok"):
        news_cap = int(news.get("cap", 10))
    else:
        news_cap = 3

    final_score = max(
        1,
        min(10, min(technical_score, news_cap)),
    )

    action, action_reason = SCORE_GUIDE[final_score]
    active_status = active_bot_status(analysis)

    def compose(max_events):
        lines = [
            f"SOL/USDT — PUNTEGGIO {final_score}/10",
        ]

        if active_status:
            lines.append(
                "AZIONE: NON APRIRE UN SECONDO BOT"
            )
            lines.append(f"STATO BOT: {active_status}")
        else:
            lines.append(
                f"AZIONE: {action} — {action_reason}"
            )

        if final_score >= 7 and not active_status:
            lines.extend(
                [
                    "",
                    "PARAMETRI GRID",
                    f"Lower: {analysis['lower']:.2f} | "
                    f"Upper: {analysis['upper']:.2f}",
                    f"Griglie: {analysis['grids']} | "
                    f"passo: {analysis['step_pct']:.2f}%",
                    f"Investimento: ~{analysis['invest_usdt']:.0f} USDT | "
                    f"~{analysis['per_grid']:.1f} USDT/griglia",
                    f"Stop loss indicativo: {analysis['stop_loss']:.2f}",
                ]
            )

        lines.extend(
            [
                "",
                "PERCHÉ QUESTO PUNTEGGIO",
                f"• Tecnica: somma contributi "
                f"{analysis['raw_score']}, "
                f"punteggio tecnico {technical_score}/10.",
            ]
        )

        for reason in analysis["score_reasons"]:
            lines.append(f"• {reason}")

        if analysis.get("score_clamp_note"):
            lines.append(f"• {analysis['score_clamp_note']}")

        lines.append(
            f"• News: limite massimo {news_cap}/10; "
            f"punteggio finale min({technical_score}, {news_cap}) "
            f"= {final_score}/10."
        )

        lines.extend(
            [
                "",
                f"NEWS MACRO VALUTATE "
                f"({NEWS_COUNTRIES_LABEL}, {LOCAL_TZ_NAME})",
                *format_news_lines(news, max_events=max_events),
                "",
                "SCALA OPERATIVA 1–10",
            ]
        )

        for level in range(1, 11):
            level_action, level_reason = SCORE_GUIDE[level]
            lines.append(
                f"{level}/10 — {level_action}: {level_reason}."
            )

        lines.extend(
            [
                "",
                "La scala è un indice, non una probabilità. "
                "Questo script non invia ordini a Pionex.",
            ]
        )

        return "\n".join(lines)

    report = compose(MAX_NEWS_EVENTS_IN_REPORT)

    # Mantiene il messaggio entro un margine dal limite Telegram.
    if len(report) > 3900:
        report = compose(5)

    if len(report) > 3900:
        report = report[:3890].rsplit("\n", 1)[0]

    return report


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
        print(
            "Telegram non configurato: imposta "
            "TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID."
        )
        return

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={
                "chat_id": TG_ID,
                "text": report,
            },
            timeout=15,
        )

        print(f"Telegram HTTP {response.status_code}")

        if response.status_code != 200:
            print("Invio Telegram non riuscito.")
            sys.exit(1)

    except requests.RequestException as exc:
        print(f"Errore Telegram ({type(exc).__name__}).")
        sys.exit(1)


if __name__ == "__main__":
    main()
