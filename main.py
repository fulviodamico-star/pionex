import os
import sys
import math
import re
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Gemini è opzionale.
try:
    from google import genai
except ImportError:
    genai = None


# ---------------------------------------------------------------------
# CONFIGURAZIONE
# ---------------------------------------------------------------------

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Range di una griglia già attiva su Pionex, se configurato.
ACTIVE_LOWER = os.getenv("ACTIVE_LOWER", "").strip()
ACTIVE_UPPER = os.getenv("ACTIVE_UPPER", "").strip()

# Paesi del calendario economico. Default: USA.
# Esempio: NEWS_COUNTRIES=US,CA,GB
COUNTRY_ALIASES = {
    "usa": "us",
    "united states": "us",
    "united states of america": "us",
}


def normalize_country(value):
    value = str(value or "").strip().lower()
    return COUNTRY_ALIASES.get(value, value)


NEWS_COUNTRIES = {
    normalize_country(country)
    for country in os.getenv("NEWS_COUNTRIES", "US").split(",")
    if country.strip()
} or {"us"}

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

# Soglie iniziali per valutare la volatilità post-news.
# Non sono soglie universali: vanno verificate e calibrate.
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
    """
    Scarica candele SOL/USD da Kraken.
    Scarta l'ultima candela, che potrebbe essere ancora aperta.
    """
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
        raise RuntimeError("Kraken ha restituito una lista di candele vuota.")

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

    tr = []
    pdm = []
    mdm = []

    for i in range(1, len(close)):
        up = high[i] - high[i - 1]
        down = low[i - 1] - low[i]

        pdm.append(up if up > down and up > 0 else 0)
        mdm.append(down if down > up and down > 0 else 0)

        tr.append(
            max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i] - close[i - 1]),
            )
        )

    s_tr = sum(tr[:n])
    s_p = sum(pdm[:n])
    s_m = sum(mdm[:n])
    dxs = []

    for i in range(n, len(tr) + 1):
        if i > n:
            s_tr = s_tr - s_tr / n + tr[i - 1]
            s_p = s_p - s_p / n + pdm[i - 1]
            s_m = s_m - s_m / n + mdm[i - 1]

        if s_tr <= 0:
            dxs.append(0.0)
            continue

        pdi = 100 * s_p / s_tr
        mdi = 100 * s_m / s_tr

        if pdi + mdi == 0:
            dxs.append(0.0)
        else:
            dxs.append(100 * abs(pdi - mdi) / (pdi + mdi))

    if not dxs:
        return 0.0, 0.0

    seed = dxs[:n]
    adx_value = sum(seed) / len(seed)

    for dx in dxs[n:]:
        adx_value = (adx_value * (n - 1) + dx) / n

    return s_tr / n, adx_value


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
# ANALISI TECNICA
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

    h24, l24 = max(high[-24:]), min(low[-24:])
    h7, l7 = max(high[-168:]), min(low[-168:])

    # Escursione giornaliera media degli ultimi 7 giorni.
    daily = []

    for day in range(7):
        seg_high = high[-(day + 1) * 24: len(high) - day * 24]
        seg_low = low[-(day + 1) * 24: len(low) - day * 24]
        daily.append((max(seg_high) - min(seg_low)) / price * 100)

    avg_daily = sum(daily) / len(daily)

    # Range suggerito usando i percentili degli ultimi 7 giorni.
    lower = percentile(low[-168:], 0.10)
    upper = percentile(high[-168:], 0.90)

    lower = min(max(lower, price * 0.92), price * 0.965)
    upper = max(min(upper, price * 1.08), price * 1.035)

    width_pct = (upper - lower) / price * 100
    invest_usdt = INVEST_EUR * get_eurusd()

    max_by_money = max(1, int(invest_usdt // MIN_ORDER_USDT))
    target_grids = max(5, min(50, round(width_pct / TARGET_STEP_PCT)))
    grids = min(target_grids, max_by_money)

    step_pct = width_pct / grids
    stop_loss = lower * 0.975
    pos = (price - lower) / (upper - lower) * 100

    # Punteggio tecnico.
    score = 0
    why = []

    if adx < 20:
        score += 2
        why.append(
            f"✅ ADX {adx:.0f}: mercato laterale, ideale per la griglia"
        )
    elif adx < 25:
        score += 1
        why.append(f"🟡 ADX {adx:.0f}: trend debole, accettabile")
    elif adx < 30:
        score -= 1
        why.append(
            f"⚠️ ADX {adx:.0f}: trend in formazione, "
            "rischio uscita dal range"
        )
    else:
        score -= 3
        why.append(f"❌ ADX {adx:.0f}: trend forte, la griglia soffre")

    gap = abs(ema20 - ema50) / price * 100
    direction = "rialzista" if ema20 > ema50 else "ribassista"

    if gap < 1:
        score += 1
        why.append(
            f"✅ EMA20 ed EMA50 vicine ({gap:.1f}%): nessun trend netto"
        )
    elif gap > 3:
        score -= 2
        why.append(
            f"❌ Trend {direction} marcato "
            f"(EMA20/50 distanti {gap:.1f}%)"
        )
    else:
        why.append(
            f"🟡 Leggera tendenza {direction} "
            f"(EMA20/50 distanti {gap:.1f}%)"
        )

    if 40 <= rsi14 <= 60:
        score += 1
        why.append(f"✅ RSI {rsi14:.0f}: neutro")
    elif rsi14 > 70 or rsi14 < 30:
        score -= 2
        state = "ipercomprato" if rsi14 > 70 else "ipervenduto"
        why.append(
            f"❌ RSI {rsi14:.0f}: {state}, possibile movimento brusco"
        )
    else:
        why.append(f"🟡 RSI {rsi14:.0f}: lievemente sbilanciato")

    if 25 <= pos <= 75:
        score += 1
        why.append(f"✅ Prezzo in zona centrale del range ({pos:.0f}%)")
    else:
        score -= 1
        why.append(f"⚠️ Prezzo vicino a un estremo del range ({pos:.0f}%)")

    if avg_daily >= 3:
        score += 1
        why.append(
            f"✅ Escursione media giornaliera {avg_daily:.1f}%: "
            "abbastanza movimento"
        )
    elif avg_daily < 1.5:
        score -= 1
        why.append(
            f"⚠️ Escursione media giornaliera {avg_daily:.1f}%: "
            "poco movimento, profitti bassi"
        )
    else:
        why.append(f"🟡 Escursione media giornaliera {avg_daily:.1f}%")

    if abs(chg24) > 8:
        score -= 2
        why.append(
            f"❌ Variazione 24h {chg24:+.1f}%: mercato troppo nervoso"
        )

    if step_pct < MIN_STEP_PCT:
        score -= 1
        why.append(
            f"⚠️ Passo griglia {step_pct:.2f}%: troppo stretto con "
            f"{INVEST_EUR}€, le fee lo erodono"
        )

    return {
        "price": price,
        "ema20": ema20,
        "ema50": ema50,
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
        "pos": pos,
        "score": score,
        "why": why,
        "invest_usdt": invest_usdt,
        "width_pct": width_pct,
        "per_grid": invest_usdt / grids,
        "candle_times": candles["time"],
    }


# ---------------------------------------------------------------------
# CALENDARIO FINNHUB
# ---------------------------------------------------------------------

def safe_finnhub_detail(value):
    """Pulisce il messaggio Finnhub prima di inserirlo nel log."""
    text = str(value or "").strip()

    if FINNHUB_API_KEY:
        text = text.replace(FINNHUB_API_KEY, "[chiave omessa]")

    text = re.sub(
        r"([?&]token=)[^&\s\"']+",
        r"\1[omesso]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"[\r\n\t]+", " ", text)

    return text[:300] or "nessun dettaglio fornito"


def finnhub_http_error(status_code, payload):
    detail = None

    if isinstance(payload, dict):
        detail = (
            payload.get("error")
            or payload.get("message")
            or payload.get("detail")
        )

    if not detail:
        detail = "risposta non JSON o senza dettagli"

    hints = {
        401: (
            "Controlla la validità della chiave e che il workflow la passi "
            "come FINNHUB_API_KEY."
        ),
        403: (
            "L'account o il piano Finnhub potrebbe non avere accesso "
            "a questo endpoint."
        ),
        429: "È stato raggiunto il limite di richieste Finnhub.",
    }

    detail = safe_finnhub_detail(detail)
    hint = hints.get(status_code, "")

    if hint:
        return f"Finnhub HTTP {status_code}: {detail}. {hint}"

    return f"Finnhub HTTP {status_code}: {detail}"


def parse_calendar_time(value):
    """Interpreta l'orario Finnhub come UTC se non ha un fuso esplicito."""
    raw = str(value).strip()

    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"

    result = datetime.fromisoformat(raw)

    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)

    return result.astimezone(timezone.utc)


def fetch_macro_events(now):
    """
    Recupera il calendario economico Finnhub per i paesi configurati.
    """
    if not FINNHUB_API_KEY:
        raise RuntimeError(
            "FINNHUB_API_KEY assente nell'ambiente. "
            "Controlla che il workflow GitHub Actions la passi al job."
        )

    today_local = now.astimezone(LOCAL_TZ).date()

    params = {
        "from": (today_local - timedelta(days=1)).isoformat(),
        "to": (today_local + timedelta(days=1)).isoformat(),
        "token": FINNHUB_API_KEY,
    }

    try:
        response = requests.get(
            "https://finnhub.io/api/v1/calendar/economic",
            params=params,
            timeout=15,
        )
    except requests.Timeout:
        raise RuntimeError("Timeout durante la richiesta a Finnhub.") from None
    except requests.RequestException as exc:
        # Non stampare il testo della richiesta: potrebbe contenere la chiave.
        raise RuntimeError(
            f"Errore di rete verso Finnhub ({type(exc).__name__})."
        ) from None

    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError(
            finnhub_http_error(response.status_code, None)
        ) from None

    if response.status_code != 200:
        raise RuntimeError(
            finnhub_http_error(response.status_code, payload)
        )

    if not isinstance(payload, dict):
        raise RuntimeError(
            "Finnhub HTTP 200: formato della risposta non valido."
        )

    if payload.get("error"):
        raise RuntimeError(
            "Finnhub: " + safe_finnhub_detail(payload["error"])
        )

    calendar = payload.get("economicCalendar")

    if not isinstance(calendar, list):
        received_keys = ", ".join(str(key) for key in payload.keys())
        raise RuntimeError(
            "Finnhub HTTP 200: campo 'economicCalendar' assente o non valido. "
            f"Campi ricevuti: {received_keys or 'nessuno'}."
        )

    events = []

    for item in calendar:
        country = str(item.get("country") or "").strip()

        if normalize_country(country) not in NEWS_COUNTRIES:
            continue

        if not item.get("time"):
            continue

        try:
            event_time = parse_calendar_time(item["time"])
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
                "country": country,
                "impact": str(item.get("impact") or "unknown")
                .strip()
                .lower(),
                "name": str(item.get("event") or "Evento"),
                "actual": item.get("actual"),
                "estimate": item.get("estimate"),
                "previous": item.get("prev"),
            }
        )

    return sorted(events, key=lambda event: event["dt"])


def get_post_news_reaction(event):
    """
    Misura la reazione di SOL dopo l'evento con candele da 5 minuti.
    Non prova a interpretare il testo della notizia.
    """
    candles = get_candles(interval=5)
    times = candles["time"]
    closes = candles["close"]

    if len(closes) < 4:
        return None

    interval_seconds = 5 * 60
    release_ts = event["dt"].timestamp()

    # Prima candela da 5 minuti che inizia all'orario dell'evento o dopo.
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

    # Se i dati sono troppo vecchi, non usarli come conferma.
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

    # Le ultime tre candele coprono 15 minuti.
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
            error = (
                "Errore durante la lettura del calendario "
                f"({type(exc).__name__})."
            )

        print(f"⚠️ Calendario Finnhub: {error}")

        return {
            "ok": False,
            "key_configured": bool(FINNHUB_API_KEY),
            "state": "unknown",
            "events": [],
            "event": None,
            "reaction": None,
            "now": now,
            "error": error,
        }

    result = {
        "ok": True,
        "key_configured": True,
        "state": "clear",
        "events": events,
        "event": None,
        "reaction": None,
        "now": now,
        "error": None,
    }

    high_impact = [
        event for event in events
        if event["impact"] == "high"
    ]

    # Blocca nuove aperture nei minuti prima di una news importante.
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

    # Attendi il cooldown subito dopo l'uscita della notizia.
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

    # Dopo il cooldown aspetta una candela oraria completa successiva alla news.
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
                "⚠️ Reazione post-news non disponibile "
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
# DECISIONE
# ---------------------------------------------------------------------

def decide(analysis, news):
    """Verdetto e istruzioni. Il filtro news riguarda le nuove aperture."""

    active_outside = None

    # Se esiste una griglia attiva, controlla prima il range.
    if ACTIVE_LOWER and ACTIVE_UPPER:
        try:
            active_lower = float(ACTIVE_LOWER)
            active_upper = float(ACTIVE_UPPER)
            price = analysis["price"]

            if active_lower >= active_upper:
                raise ValueError("Range attivo non valido")

            if price < active_lower or price > active_upper:
                active_outside = (
                    f"Il prezzo ({price:.2f}) è fuori dal range attivo "
                    f"({active_lower:.2f}–{active_upper:.2f})."
                )
            else:
                edge = min(
                    price - active_lower,
                    active_upper - price,
                ) / price * 100

                if edge < 1.5:
                    return (
                        "🟠 VALUTA DI RICENTRARE",
                        f"Prezzo a {edge:.1f}% da un estremo del tuo range "
                        f"({active_lower:.2f}–{active_upper:.2f}). "
                        "Controlla il bot su Pionex. Per una nuova apertura, "
                        "rispetta anche il filtro news.",
                    )

                return (
                    "🟢 NON TOCCARE NULLA",
                    f"Il prezzo è dentro il tuo range attivo "
                    f"({active_lower:.2f}–{active_upper:.2f}) e lontano "
                    "dai bordi. Questo script non modifica il bot su Pionex.",
                )

        except (ValueError, TypeError):
            active_outside = None

    def add_active_context(text):
        if active_outside:
            return active_outside + "\n\n" + text
        return text

    state = news.get("state") if news else "unknown"

    if state == "unknown":
        return (
            "🟠 CALENDARIO NEWS NON VERIFICATO",
            add_active_context(
                "Non avviare una nuova griglia: il calendario non è stato "
                "verificato. Controlla FINNHUB_API_KEY e il workflow "
                "GitHub Actions."
            ),
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
            "⏸️ ATTESA NEWS",
            add_active_context(
                f"Evento ad alto impatto: {event['name']} alle "
                f"{event['local_dt'].strftime('%d/%m %H:%M')} "
                f"({LOCAL_TZ_NAME}), tra circa {minutes} minuti. "
                "Non avviare una nuova griglia prima dell'evento."
            ),
        )

    if state == "cooldown":
        event = news["event"]
        elapsed = (news["now"] - event["dt"]).total_seconds() / 60
        remaining = max(0, math.ceil(NEWS_COOLDOWN_MIN - elapsed))

        return (
            "⏸️ ATTESA REAZIONE POST-NEWS",
            add_active_context(
                f"È appena uscito {event['name']}. Attendi ancora circa "
                f"{remaining} minuti prima di valutare una nuova griglia."
            ),
        )

    if state == "wait_hour":
        return (
            "⏸️ ATTESA CHIUSURA ORARIA",
            add_active_context(
                "È passato il cooldown, ma manca ancora una candela oraria "
                "completa successiva alla news. Rivaluta alla prossima "
                "esecuzione."
            ),
        )

    if state == "unstable":
        reaction = news["reaction"]

        return (
            "🟠 ATTESA: MERCATO ANCORA VOLATILE",
            add_active_context(
                f"Reazione SOL dal prezzo pre-news: "
                f"{reaction['reaction_pct']:+.2f}%. Ultimi 15 minuti: "
                f"movimento {reaction['move_15m_pct']:+.2f}%, range "
                f"{reaction['range_15m_pct']:.2f}%. Aspetta che il "
                "movimento si calmi."
            ),
        )

    if state == "reaction_unknown":
        return (
            "🟠 REAZIONE NEWS NON VERIFICATA",
            add_active_context(
                "Non è stato possibile leggere le candele SOL/USD successive "
                "alla news. Per prudenza non avviare una nuova griglia."
            ),
        )

    if state not in ("clear", "post_clear"):
        return (
            "🟠 ATTESA NEWS",
            add_active_context(
                "Stato del calendario non verificato: non avviare una "
                "nuova griglia."
            ),
        )

    score = analysis["score"]

    if active_outside:
        if score >= 4:
            verdict = "🔴 FUORI RANGE — VALUTA IL RIAVVIO"
            instructions = (
                f"{active_outside}\n"
                "Il punteggio tecnico della nuova analisi è favorevole, "
                "ma questo script non chiude né ricrea bot su Pionex. "
                "Verifica manualmente ordini e posizione."
            )
        elif score >= 1:
            verdict = "🟠 FUORI RANGE — ASPETTA"
            instructions = (
                f"{active_outside}\n"
                "Le condizioni tecniche sono miste: rivaluta al prossimo "
                "report prima di creare una nuova griglia."
            )
        else:
            verdict = "🔴 FUORI RANGE — NON RIAVVIARE"
            instructions = (
                f"{active_outside}\n"
                "Le condizioni sono sfavorevoli per una nuova griglia. "
                "Controlla manualmente il bot su Pionex."
            )

    elif score >= 4:
        verdict = "🟢 AVVIA IL GRID BOT"
        instructions = (
            "Condizioni favorevoli. Puoi valutare la creazione del bot "
            "su Pionex con i parametri riportati."
        )
    elif score >= 1:
        verdict = "🟡 ASPETTA"
        instructions = (
            "Condizioni miste: aspetta il prossimo report. Se decidi di "
            "entrare comunque, valuta meno capitale e uno stop loss."
        )
    else:
        verdict = "🔴 NON AVVIARE"
        instructions = (
            "Condizioni sfavorevoli per una griglia. Rivaluta al prossimo "
            "report."
        )

    if state == "post_clear":
        reaction = news["reaction"]
        instructions += (
            f"\nConferma post-news: reazione SOL "
            f"{reaction['direction']} "
            f"({reaction['reaction_pct']:+.2f}% dal prezzo pre-evento); "
            "ultimi 15 minuti classificati come stabili."
        )

    return verdict, instructions


# ---------------------------------------------------------------------
# COMMENTO GEMINI
# ---------------------------------------------------------------------

def ai_comment(analysis, verdict):
    if not GEMINI_KEY or genai is None:
        return "n/d"

    try:
        client = genai.Client(api_key=GEMINI_KEY)

        prompt = (
            "Sei un analista di trading prudente. Dati SOL/USD: "
            f"prezzo {analysis['price']:.2f}, "
            f"var 24h {analysis['chg24']:+.1f}%, "
            f"var 7g {analysis['chg7d']:+.1f}%, "
            f"ADX {analysis['adx']:.0f}, RSI {analysis['rsi']:.0f}, "
            f"EMA20 {analysis['ema20']:.2f}, "
            f"EMA50 {analysis['ema50']:.2f}, min/max 7g "
            f"{analysis['l7']:.2f}/{analysis['h7']:.2f}. "
            f"Verdetto del sistema: {verdict}. "
            "In 3 frasi in italiano spiega il contesto di mercato e il "
            "principale rischio per un grid bot. Non inventare numeri e non "
            "contraddire il verdetto. Niente markdown."
        )

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )

        text = getattr(response, "text", None)
        return text.strip() if text else "n/d"

    except Exception as exc:
        print(f"⚠️ Gemini non disponibile ({type(exc).__name__}).")
        return "n/d"


# ---------------------------------------------------------------------
# REPORT
# ---------------------------------------------------------------------

def calendar_report_lines(news):
    if not news or not news.get("ok"):
        key_configured = bool(news and news.get("key_configured"))
        error = (
            news.get("error", "errore non specificato")
            if news
            else "calendario non disponibile"
        )

        return [
            "• FINNHUB_API_KEY presente nel job: "
            f"{'sì' if key_configured else 'no'}",
            f"• Errore: {error}",
            "• Il calendario non è stato verificato.",
        ]

    today = news["now"].astimezone(LOCAL_TZ).date()

    today_events = [
        event for event in news.get("events", [])
        if event["local_dt"].date() == today
    ]

    lines = []

    if not today_events:
        lines.append(
            "• Finnhub ha risposto correttamente, ma non ha restituito "
            f"eventi per {NEWS_COUNTRIES_LABEL} oggi."
        )
    else:
        for event in today_events[:8]:
            line = (
                f"• {event['local_dt'].strftime('%H:%M')} "
                f"{event['country']} {event['impact'].upper()} — "
                f"{event['name']}"
            )

            details = []

            if event["estimate"] not in (None, ""):
                details.append(f"stima {event['estimate']}")

            if event["actual"] not in (None, ""):
                details.append(f"effettivo {event['actual']}")

            if event["previous"] not in (None, ""):
                details.append(f"precedente {event['previous']}")

            if details:
                line += " (" + ", ".join(details) + ")"

            lines.append(line)

        if len(today_events) > 8:
            lines.append(
                f"• Altri {len(today_events) - 8} eventi non mostrati."
            )

    reaction = news.get("reaction")

    if reaction:
        stability = "stabile" if reaction["stable"] else "ancora volatile"
        lines.append(
            f"• Reazione SOL post-news: {reaction['direction']} "
            f"{reaction['reaction_pct']:+.2f}%; ultimi 15m "
            f"{reaction['move_15m_pct']:+.2f}%, range "
            f"{reaction['range_15m_pct']:.2f}% ({stability})."
        )

    return lines


def build_report(analysis, news):
    verdict, instructions = decide(analysis, news)
    comment = ai_comment(analysis, verdict)

    lines = [
        "🤖 PIONEX GRID – SOL/USDT",
        "",
        verdict,
        instructions,
        "",
        "📋 PARAMETRI (solo se il verdetto consente l'avvio)",
        f"• Lower: {analysis['lower']:.2f}",
        f"• Upper: {analysis['upper']:.2f}",
        f"• Grids: {analysis['grids']} "
        f"(passo ~{analysis['step_pct']:.2f}%)",
        f"• Investimento: ~{analysis['invest_usdt']:.0f} USDT "
        f"(≈{analysis['per_grid']:.1f} USDT per griglia)",
        f"• Stop loss indicativo: {analysis['stop']:.2f}",
        "",
        f"📰 CALENDARIO ECONOMICO "
        f"({NEWS_COUNTRIES_LABEL}, {LOCAL_TZ_NAME})",
        *calendar_report_lines(news),
        "",
        "📊 MERCATO",
        f"• Prezzo: ${analysis['price']:.2f} "
        f"({analysis['chg24']:+.1f}% 24h | {analysis['chg7d']:+.1f}% 7g)",
        f"• Min/Max 24h: {analysis['l24']:.2f} / {analysis['h24']:.2f}",
        f"• Min/Max 7g: {analysis['l7']:.2f} / {analysis['h7']:.2f}",
        f"• Posizione nel range proposto: {analysis['pos']:.0f}% "
        "(0=basso, 100=alto)",
        "",
        f"🔎 PERCHÉ (punteggio {analysis['score']}/6)",
        *analysis["why"],
        "",
        f"📝 {comment}",
        "",
        "ℹ️ Questo script invia una valutazione: non invia ordini a Pionex.",
        "⚠️ Non è un consiglio finanziario. Se il prezzo esce dal range, "
        "il bot può restare con monete o USDT bloccati: usa solo soldi che "
        "puoi permetterti di perdere.",
    ]

    return "\n".join(lines)


# ---------------------------------------------------------------------
# AVVIO
# ---------------------------------------------------------------------

def main():
    # Mostra solo se la variabile c'è, mai il suo valore.
    print(
        "Finnhub API key configurata:",
        "sì" if FINNHUB_API_KEY else "NO",
    )

    try:
        analysis = analyze()
    except Exception as exc:
        print(f"❌ Errore analisi ({type(exc).__name__}): {exc}")
        sys.exit(1)

    news = get_news_context(analysis.get("candle_times"))
    report = build_report(analysis, news)

    print(report)

    if TG_TOKEN and TG_ID:
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={
                    "chat_id": TG_ID,
                    "text": report,
                },
                timeout=15,
            )

            print(f"Telegram: HTTP {response.status_code}")

            if response.status_code != 200:
                print(f"Errore Telegram: {response.text}")
                sys.exit(1)

        except requests.RequestException as exc:
            print(f"❌ Errore Telegram ({type(exc).__name__}).")
            sys.exit(1)
    else:
        print(
            "⚠️ Telegram non configurato: imposta "
            "TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID."
        )


if __name__ == "__main__":
    main()
