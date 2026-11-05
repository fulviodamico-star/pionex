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

# Feed pubblico del calendario economico: non richiede API key.
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Paesi da includere nel calendario. Default: USD.
# Puoi impostare, ad esempio, NEWS_COUNTRIES=USD,EUR
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

# Soglie per valutare se la reazione di SOL dopo una news si è calmata.
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


# ---------------------------------------------------------------------
# DATI DI MERCATO
# ---------------------------------------------------------------------

def get_candles(interval=60):
    """Scarica candele SOL/USD da Kraken e scarta l'ultima candela."""
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
    if not values:
        raise ValueError("Lista vuota per il calcolo EMA.")

    k = 2 / (n + 1)
    result = values[0]

    for value in values[1:]:
        result = value * k + result * (1 - k)

    return result


def rsi(closes, n=14):
    if len(closes) <= n:
        raise ValueError("Dati insufficienti per il calcolo RSI.")

    gains = []
    losses = []

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
    if len(close) <= n:
        raise ValueError("Dati insufficienti per ATR/ADX.")

    true_ranges = []
    positive_dm = []
    negative_dm = []

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
# ANALISI E PUNTEGGIO TECNICO 1-10
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
    atr, adx = atr_adx(high[-200:], low[-200:], close[-200:])
    atr_pct = atr / price * 100

    chg24 = (price / close[-25] - 1) * 100
    chg7d = (price / close[-169] - 1) * 100

    h24 = max(high[-24:])
    l24 = min(low[-24:])
    h7 = max(high[-168:])
    l7 = min(low[-168:])

    # Escursione media giornaliera degli ultimi 7 giorni.
    daily_ranges = []

    for day in range(7):
        segment_high = high[-(day + 1) * 24: len(high) - day * 24]
        segment_low = low[-(day + 1) * 24: len(low) - day * 24]

        daily_ranges.append(
            (max(segment_high) - min(segment_low)) / price * 100
        )

    avg_daily = sum(daily_ranges) / len(daily_ranges)

    # Range suggerito in base ai percentili degli ultimi 7 giorni.
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

    # Cinque indicatori, ognuno con un massimo di 2 punti.
    technical_score = 0
    ema_gap_pct = abs(ema20 - ema50) / price * 100

    # ADX: preferenza per un mercato senza trend forte.
    if adx < 20:
        technical_score += 2
    elif adx < 25:
        technical_score += 1

    # EMA: preferenza per EMA20 ed EMA50 vicine.
    if ema_gap_pct < 1:
        technical_score += 2
    elif ema_gap_pct < 3:
        technical_score += 1

    # RSI: preferenza per una condizione neutra.
    if 40 <= rsi14 <= 60:
        technical_score += 2
    elif 30 <= rsi14 <= 70:
        technical_score += 1

    # Posizione del prezzo nel range suggerito.
    if 35 <= position_in_range <= 65:
        technical_score += 2
    elif 25 <= position_in_range <= 75:
        technical_score += 1

    # Movimento sufficiente, ma non eccessivo.
    if 3 <= avg_daily <= 8:
        technical_score += 2
    elif 1.5 <= avg_daily < 3:
        technical_score += 1

    # Penalità per mercato molto nervoso o griglia troppo stretta.
    if abs(chg24) > 8:
        technical_score -= 2

    if step_pct < MIN_STEP_PCT:
        technical_score -= 1

    technical_score = max(1, min(10, technical_score))

    return {
        "price": price,
        "ema20": ema20,
        "ema50": ema50,
        "ema_gap_pct": ema_gap_pct,
        "rsi": rsi14,
        "adx": adx,
        "atr_pct": atr_pct,
        "chg24": chg24,
        "chg7d": chg7d,
        "h24": h24,
        "l24": l24,
        "h7": h7,
        "l7": l7,
        "avg_daily": avg_daily,
        "lower": lower,
        "upper": upper,
        "grids": grids,
        "step_pct": step_pct,
        "stop": stop_loss,
        "pos": position_in_range,
        "score": technical_score,
        "invest_usdt": invest_usdt,
        "per_grid": invest_usdt / grids,
        "candle_times": candles["time"],
    }


# ---------------------------------------------------------------------
# CALENDARIO ECONOMICO PUBBLICO
# ---------------------------------------------------------------------

def parse_calendar_time(value):
    """Converte l'orario del feed in UTC."""
    raw = str(value or "").strip()

    if not raw:
        raise ValueError("Orario evento mancante.")

    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"

    event_time = datetime.fromisoformat(raw)

    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)

    return event_time.astimezone(timezone.utc)


def fetch_macro_events(now):
    """Scarica il calendario settimanale pubblico, senza API key."""
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
            f"Errore di rete calendario ({type(exc).__name__})."
        ) from None

    if response.status_code != 200:
        raise RuntimeError(
            f"Feed calendario HTTP {response.status_code}."
        )

    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError("Il feed calendario non ha restituito JSON valido.") from None

    if not isinstance(payload, list):
        raise RuntimeError("Formato del feed calendario non riconosciuto.")

    today_local = now.astimezone(LOCAL_TZ).date()
    events = []

    for item in payload:
        if not isinstance(item, dict):
            continue

        country = str(item.get("country") or "").strip()

        if normalize_country(country) not in NEWS_COUNTRIES:
            continue

        if not item.get("date"):
            continue

        try:
            event_time = parse_calendar_time(item["date"])
        except (TypeError, ValueError):
            continue

        local_time = event_time.astimezone(LOCAL_TZ)
        minutes_to_event = (event_time - now).total_seconds() / 60

        is_today = local_time.date() == today_local
        relevant_for_filter = (
            -NEWS_LOOKBACK_HOURS * 60
            <= minutes_to_event
            <= NEWS_BLOCK_BEFORE_MIN
        )

        if not is_today and not relevant_for_filter:
            continue

        events.append(
            {
                "dt": event_time,
                "local_dt": local_time,
                "country": country.upper(),
                "impact": str(item.get("impact") or "unknown")
                .strip()
                .lower(),
                "name": str(
                    item.get("title")
                    or item.get("event")
                    or "Evento"
                ),
                "actual": item.get("actual"),
                "estimate": item.get("forecast") or item.get("estimate"),
                "previous": item.get("previous") or item.get("prev"),
            }
        )

    events.sort(key=lambda event: event["dt"])
    print(f"Calendario caricato: {len(events)} eventi pertinenti.")

    return events


def get_post_news_reaction(event):
    """Valuta la reazione recente di SOL dopo una news."""
    candles = get_candles(interval=5)
    times = candles["time"]
    closes = candles["close"]

    if len(closes) < 4:
        return None

    interval_seconds = 5 * 60
    release_ts = event["dt"].timestamp()

    first_post_ts = (
        math.ceil(release_ts / interval_seconds) * interval_seconds
    )

    post_indices = [
        i for i, timestamp in enumerate(times)
        if timestamp >= first_post_ts
    ]

    pre_indices = [
        i for i, timestamp in enumerate(times)
        if timestamp + interval_seconds <= release_ts
    ]

    if len(pre_indices) < 1 or len(post_indices) < 4:
        return None

    last_four = post_indices[-4:]

    # Non usare dati con intervalli mancanti.
    if any(
        times[b] - times[a] != interval_seconds
        for a, b in zip(last_four, last_four[1:])
    ):
        return None

    latest_bar_close_time = times[-1] + interval_seconds
    now_ts = datetime.now(timezone.utc).timestamp()

    if now_ts - latest_bar_close_time > 15 * 60:
        return None

    pre_price = closes[pre_indices[-1]]
    current_price = closes[last_four[-1]]

    if pre_price <= 0 or current_price <= 0:
        return None

    reaction_pct = (current_price / pre_price - 1) * 100

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
            print(f"Calendario non disponibile: {exc}")
        else:
            print(
                "Calendario non disponibile "
                f"({type(exc).__name__})."
            )

        return {
            "ok": False,
            "state": "unknown",
            "events": [],
            "event": None,
            "reaction": None,
            "now": now,
        }

    result = {
        "ok": True,
        "state": "clear",
        "events": events,
        "event": None,
        "reaction": None,
        "now": now,
    }

    high_impact = [
        event for event in events
        if event["impact"] == "high"
    ]

    # Blocca nuove aperture prima di una news importante.
    upcoming = [
        event for event in high_impact
        if timedelta(0) <= event["dt"] - now
        <= timedelta(minutes=NEWS_BLOCK_BEFORE_MIN)
    ]

    if upcoming:
        result.update(
            state="before",
            event=min(upcoming, key=lambda event: event["dt"]),
        )
        return result

    # Attendi il cooldown dopo l'uscita della news.
    recent = [
        event for event in high_impact
        if timedelta(0) <= now - event["dt"]
        < timedelta(minutes=NEWS_COOLDOWN_MIN)
    ]

    if recent:
        result.update(
            state="cooldown",
            event=max(recent, key=lambda event: event["dt"]),
        )
        return result

    # Dopo il cooldown, attendi una candela oraria completa successiva.
    post_events = [
        event for event in high_impact
        if timedelta(minutes=NEWS_COOLDOWN_MIN)
        <= now - event["dt"]
        <= timedelta(hours=NEWS_LOOKBACK_HOURS)
    ]

    if post_events:
        event = max(post_events, key=lambda item: item["dt"])

        first_full_hour = (
            math.ceil(event["dt"].timestamp() / 3600) * 3600
        )

        has_closed_post_news_hour = any(
            int(timestamp) >= first_full_hour
            for timestamp in (hourly_times or [])
        )

        if not has_closed_post_news_hour:
            result.update(
                state="wait_hour",
                event=event,
            )
            return result

        try:
            reaction = get_post_news_reaction(event)
        except Exception as exc:
            print(
                "Reazione post-news non disponibile "
                f"({type(exc).__name__})."
            )
            reaction = None

        if reaction is None:
            result.update(
                state="reaction_unknown",
                event=event,
            )
            return result

        result.update(
            state="post_clear" if reaction["stable"] else "unstable",
            event=event,
            reaction=reaction,
        )

    return result


# ---------------------------------------------------------------------
# DECISIONE E MESSAGGIO
# ---------------------------------------------------------------------

def evaluate_signal(analysis, news):
    """
    Restituisce punteggio, decisione e indicazione se mostrare i parametri.
    """

    # Se è già impostato un bot, non suggerire di aprirne un altro.
    if ACTIVE_LOWER and ACTIVE_UPPER:
        try:
            active_lower = float(ACTIVE_LOWER)
            active_upper = float(ACTIVE_UPPER)
            price = analysis["price"]

            if active_lower >= active_upper:
                raise ValueError("Range attivo non valido.")

            if price < active_lower or price > active_upper:
                return (
                    2,
                    "🔴 BOT ATTIVO FUORI RANGE — controlla Pionex",
                    False,
                )

            edge_pct = min(
                price - active_lower,
                active_upper - price,
            ) / price * 100

            if edge_pct < 1.5:
                return (
                    2,
                    f"🟠 MONITORA — bot attivo a {edge_pct:.1f}% dal bordo",
                    False,
                )

            return (
                1,
                "✅ NON FARE NULLA — bot attivo nel range",
                False,
            )

        except (ValueError, TypeError):
            print("Range attivo non valido; valuto il punteggio di ingresso.")

    score = analysis["score"]
    state = news.get("state") if news else "unknown"

    # Se il calendario non è verificabile, non dare il via libera.
    if state == "unknown":
        return (
            1,
            "🔴 NON APRIRE — calendario news non verificato",
            False,
        )

    if state == "before":
        event = news["event"]
        minutes = max(
            0,
            math.ceil(
                (event["dt"] - news["now"]).total_seconds() / 60
            ),
        )

        return (
            min(score, 3),
            f"🔴 NON APRIRE — {event['name']} alle "
            f"{event['local_dt'].strftime('%H:%M')} "
            f"(tra {minutes} min)",
            False,
        )

    if state == "cooldown":
        event = news["event"]
        elapsed = (news["now"] - event["dt"]).total_seconds() / 60
        remaining = max(
            0,
            math.ceil(NEWS_COOLDOWN_MIN - elapsed),
        )

        return (
            min(score, 2),
            f"⏸️ ATTENDI — news appena uscita; rivaluta tra {remaining} min",
            False,
        )

    if state == "wait_hour":
        return (
            min(score, 2),
            "⏸️ ATTENDI — manca la candela oraria post-news",
            False,
        )

    if state == "unstable":
        return (
            min(score, 3),
            "🔴 NON APRIRE — SOL ancora volatile dopo la news",
            False,
        )

    if state == "reaction_unknown":
        return (
            min(score, 2),
            "🔴 NON APRIRE — reazione post-news non verificata",
            False,
        )

    if state not in ("clear", "post_clear"):
        return (
            1,
            "🔴 NON APRIRE — stato news non verificato",
            False,
        )

    if score == 10:
        return score, "🟢 APRI ORA SU PIONEX", True

    if score >= 7:
        return score, "🟡 VALUTA APERTURA", True

    if score >= 4:
        return score, "⏸️ ATTENDI", False

    return score, "🔴 NON APRIRE", False


def build_report(analysis, news):
    score, action, show_parameters = evaluate_signal(analysis, news)

    lines = [
        f"SOL/USDT — {score}/10",
        action,
    ]

    if show_parameters:
        lines.extend(
            [
                f"Lower: {analysis['lower']:.2f} | "
                f"Upper: {analysis['upper']:.2f}",
                f"Griglie: {analysis['grids']} | "
                f"Investimento: ~{analysis['invest_usdt']:.0f} USDT",
                f"Stop loss indicativo: {analysis['stop']:.2f}",
            ]
        )

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
