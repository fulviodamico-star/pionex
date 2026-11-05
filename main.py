import os
import sys
import math
import requests
from google import genai

GEMINI_KEY = os.getenv("GEMINI_API_KEY")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_ID = os.getenv("TELEGRAM_CHAT_ID")

# Opzionale: la griglia che hai GIA' attiva su Pionex (variabili GitHub)
ACTIVE_LOWER = os.getenv("ACTIVE_LOWER", "").strip()
ACTIVE_UPPER = os.getenv("ACTIVE_UPPER", "").strip()

# --- Impostazioni da adattare ---
INVEST_EUR = 50
MIN_ORDER_USDT = 5.0    # verifica nell'app Pionex il minimo reale per griglia
TARGET_STEP_PCT = 0.30  # passo di griglia desiderato (le fee Pionex ~0.05% per lato)
MIN_STEP_PCT = 0.25     # sotto questo valore le commissioni mangiano il profitto


# ---------------------------------------------------------------- DATI
def get_candles():
    """Candele orarie SOL/USD da Kraken (~30 giorni). Scarta l'ultima, ancora aperta."""
    r = requests.get(
        "https://api.kraken.com/0/public/OHLC",
        params={"pair": "SOLUSD", "interval": 60},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"Kraken: {data['error']}")
    key = next(k for k in data["result"] if k != "last")
    rows = data["result"][key][:-1]
    return {
        "high": [float(x[2]) for x in rows],
        "low": [float(x[3]) for x in rows],
        "close": [float(x[4]) for x in rows],
    }


def get_eurusd():
    try:
        r = requests.get("https://api.kraken.com/0/public/Ticker?pair=EURUSD", timeout=10)
        t = next(iter(r.json()["result"].values()))
        return float(t["c"][0])
    except Exception:
        return 1.08


# ---------------------------------------------------------- INDICATORI
def ema(values, n):
    k = 2 / (n + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def rsi(closes, n=14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag = (ag * (n - 1) + g) / n
        al = (al * (n - 1) + l) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def atr_adx(high, low, close, n=14):
    tr, pdm, mdm = [], [], []
    for i in range(1, len(close)):
        up = high[i] - high[i - 1]
        dn = low[i - 1] - low[i]
        pdm.append(up if up > dn and up > 0 else 0)
        mdm.append(dn if dn > up and dn > 0 else 0)
        tr.append(max(high[i] - low[i],
                      abs(high[i] - close[i - 1]),
                      abs(low[i] - close[i - 1])))
    s_tr, s_p, s_m = sum(tr[:n]), sum(pdm[:n]), sum(mdm[:n])
    dxs = []
    for i in range(n, len(tr) + 1):
        if i > n:
            s_tr = s_tr - s_tr / n + tr[i - 1]
            s_p = s_p - s_p / n + pdm[i - 1]
            s_m = s_m - s_m / n + mdm[i - 1]
        pdi = 100 * s_p / s_tr
        mdi = 100 * s_m / s_tr
        dxs.append(0 if pdi + mdi == 0 else 100 * abs(pdi - mdi) / (pdi + mdi))
    adx = sum(dxs[:n]) / n
    for dx in dxs[n:]:
        adx = (adx * (n - 1) + dx) / n
    return s_tr / n, adx  # ATR (Wilder), ADX


def percentile(values, p):
    s = sorted(values)
    return s[min(len(s) - 1, max(0, int(round((len(s) - 1) * p))))]


# ------------------------------------------------------------- ANALISI
def analyze():
    c = get_candles()
    hi, lo, cl = c["high"], c["low"], c["close"]
    price = cl[-1]

    ema20, ema50 = ema(cl[-200:], 20), ema(cl[-200:], 50)
    rsi14 = rsi(cl[-200:])
    atr, adx = atr_adx(hi[-200:], lo[-200:], cl[-200:])
    atr_pct = atr / price * 100

    chg24 = (price / cl[-25] - 1) * 100
    chg7d = (price / cl[-169] - 1) * 100
    h24, l24 = max(hi[-24:]), min(lo[-24:])
    h7, l7 = max(hi[-168:]), min(lo[-168:])

    # escursione giornaliera media ultimi 7 giorni
    daily = []
    for d in range(7):
        seg_h = hi[-(d + 1) * 24: len(hi) - d * 24]
        seg_l = lo[-(d + 1) * 24: len(lo) - d * 24]
        daily.append((max(seg_h) - min(seg_l)) / price * 100)
    avg_daily = sum(daily) / len(daily)

    # --- Range proposto: 7 giorni "puliti", con limiti di larghezza
    lower = percentile(lo[-168:], 0.10)
    upper = percentile(hi[-168:], 0.90)
    lower = min(max(lower, price * 0.92), price * 0.965)
    upper = max(min(upper, price * 1.08), price * 1.035)

    width_pct = (upper - lower) / price * 100
    invest_usdt = INVEST_EUR * get_eurusd()
    max_by_money = int(invest_usdt // MIN_ORDER_USDT)
    grids = max(5, min(50, round(width_pct / TARGET_STEP_PCT), max_by_money))
    step_pct = width_pct / grids
    stop_loss = lower * 0.975
    pos = (price - lower) / (upper - lower) * 100  # posizione nel range, 0-100

    # --- Punteggio
    score, why = 0, []

    if adx < 20:
        score += 2; why.append(f"✅ ADX {adx:.0f}: mercato laterale, ideale per la griglia")
    elif adx < 25:
        score += 1; why.append(f"🟡 ADX {adx:.0f}: trend debole, accettabile")
    elif adx < 30:
        score -= 1; why.append(f"⚠️ ADX {adx:.0f}: trend in formazione, rischio uscita dal range")
    else:
        score -= 3; why.append(f"❌ ADX {adx:.0f}: trend forte, la griglia soffre")

    gap = abs(ema20 - ema50) / price * 100
    direction = "rialzista" if ema20 > ema50 else "ribassista"
    if gap < 1:
        score += 1; why.append(f"✅ EMA20 ed EMA50 vicine ({gap:.1f}%): nessun trend netto")
    elif gap > 3:
        score -= 2; why.append(f"❌ Trend {direction} marcato (EMA20/50 distanti {gap:.1f}%)")
    else:
        why.append(f"🟡 Leggera tendenza {direction} (EMA20/50 distanti {gap:.1f}%)")

    if 40 <= rsi14 <= 60:
        score += 1; why.append(f"✅ RSI {rsi14:.0f}: neutro")
    elif rsi14 > 70 or rsi14 < 30:
        score -= 2
        stato = "ipercomprato" if rsi14 > 70 else "ipervenduto"
        why.append(f"❌ RSI {rsi14:.0f}: {stato}, possibile movimento brusco")
    else:
        why.append(f"🟡 RSI {rsi14:.0f}: lievemente sbilanciato")

    if 25 <= pos <= 75:
        score += 1; why.append(f"✅ Prezzo in zona centrale del range ({pos:.0f}%)")
    else:
        score -= 1; why.append(f"⚠️ Prezzo vicino a un estremo del range ({pos:.0f}%)")

    if avg_daily >= 3:
        score += 1; why.append(f"✅ Escursione media giornaliera {avg_daily:.1f}%: abbastanza movimento")
    elif avg_daily < 1.5:
        score -= 1; why.append(f"⚠️ Escursione media giornaliera {avg_daily:.1f}%: poco movimento, profitti bassi")
    else:
        why.append(f"🟡 Escursione media giornaliera {avg_daily:.1f}%")

    if abs(chg24) > 8:
        score -= 2; why.append(f"❌ Variazione 24h {chg24:+.1f}%: mercato troppo nervoso")

    if step_pct < MIN_STEP_PCT:
        score -= 1
        why.append(f"⚠️ Passo griglia {step_pct:.2f}%: troppo stretto con {INVEST_EUR}€, le fee lo erodono")

    return dict(
        price=price, ema20=ema20, ema50=ema50, rsi=rsi14, adx=adx, atr_pct=atr_pct,
        chg24=chg24, chg7d=chg7d, h24=h24, l24=l24, h7=h7, l7=l7, avg_daily=avg_daily,
        lower=lower, upper=upper, grids=grids, step_pct=step_pct, stop=stop_loss,
        pos=pos, score=score, why=why, invest_usdt=invest_usdt, width_pct=width_pct,
        per_grid=invest_usdt / grids,
    )


def decide(a):
    """Verdetto + istruzioni pratiche."""
    # Griglia gia' attiva: gestiscila
    if ACTIVE_LOWER and ACTIVE_UPPER:
        try:
            al, au = float(ACTIVE_LOWER), float(ACTIVE_UPPER)
            p = a["price"]
            if p < al or p > au:
                return ("🔴 RIAVVIA LA GRIGLIA",
                        f"Il prezzo ({p:.2f}) è FUORI dal tuo range attivo ({al:.2f}–{au:.2f}).\n"
                        f"1. Chiudi il bot attuale su Pionex.\n"
                        f"2. Se il verdetto sotto è AVVIA, creane uno nuovo con i parametri qui sotto.")
            edge = min(p - al, au - p) / p * 100
            if edge < 1.5:
                return ("🟠 VALUTA DI RICENTRARE",
                        f"Prezzo a {edge:.1f}% da un estremo del tuo range ({al:.2f}–{au:.2f}).\n"
                        f"Se tocca il bordo, chiudi e rifai la griglia con i valori sotto.")
            return ("🟢 NON TOCCARE NULLA",
                    f"Il prezzo è dentro il tuo range attivo ({al:.2f}–{au:.2f}) e lontano dai bordi.\n"
                    f"Lascia lavorare il bot: chiuderlo adesso costa solo commissioni.")
        except ValueError:
            pass

    s = a["score"]
    if s >= 4:
        return ("🟢 AVVIA IL GRID BOT",
                "Condizioni favorevoli. Crea il bot su Pionex con i parametri qui sotto.")
    if s >= 1:
        return ("🟡 ASPETTA",
                "Condizioni miste: non c'è fretta. Aspetta il prossimo report; "
                "se vuoi entrare comunque, usa meno capitale e lo stop loss.")
    return ("🔴 NON AVVIARE",
            "Condizioni sfavorevoli per una griglia. Tieni i soldi fermi e riprova al prossimo report.")


def ai_comment(a, verdict):
    if not GEMINI_KEY:
        return "n/d"
    try:
        client = genai.Client(api_key=GEMINI_KEY)
        prompt = (
            "Sei un analista di trading prudente. Dati reali SOL/USD: "
            f"prezzo {a['price']:.2f}, var 24h {a['chg24']:+.1f}%, var 7g {a['chg7d']:+.1f}%, "
            f"ADX {a['adx']:.0f}, RSI {a['rsi']:.0f}, EMA20 {a['ema20']:.2f}, EMA50 {a['ema50']:.2f}, "
            f"min/max 7g {a['l7']:.2f}/{a['h7']:.2f}. Verdetto del sistema: {verdict}. "
            "In 3 frasi in italiano spiega il contesto di mercato e il principale rischio per un "
            "grid bot. Non inventare numeri e non contraddire il verdetto. Niente markdown."
        )
        res = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return res.text.strip()
    except Exception as e:
        print(f"⚠️ Gemini non disponibile: {e}")
        return "n/d"


def build_report(a):
    verdict, instructions = decide(a)
    comment = ai_comment(a, verdict)
    lines = [
        "🤖 PIONEX GRID – SOL/USDT",
        "",
        f"{verdict}",
        instructions,
        "",
        "📋 PARAMETRI DA INSERIRE (modalità Manual)",
        f"• Lower: {a['lower']:.2f}",
        f"• Upper: {a['upper']:.2f}",
        f"• Grids: {a['grids']} (passo ~{a['step_pct']:.2f}%)",
        f"• Investimento: ~{a['invest_usdt']:.0f} USDT (≈{a['per_grid']:.1f} USDT per griglia)",
        f"• Stop loss: {a['stop']:.2f}",
        "",
        "📊 MERCATO",
        f"• Prezzo: ${a['price']:.2f} ({a['chg24']:+.1f}% 24h | {a['chg7d']:+.1f}% 7g)",
        f"• Min/Max 24h: {a['l24']:.2f} / {a['h24']:.2f}",
        f"• Min/Max 7g: {a['l7']:.2f} / {a['h7']:.2f}",
        f"• Posizione nel range proposto: {a['pos']:.0f}% (0=basso, 100=alto)",
        "",
        f"🔎 PERCHÉ (punteggio {a['score']}/6)",
        *a["why"],
        "",
        f"📝 {comment}",
        "",
        "⚠️ Non è un consiglio finanziario. Se il prezzo esce dal range, il bot resta con "
        "monete o USDT bloccati: usa solo soldi che puoi permetterti di perdere.",
    ]
    return "\n".join(lines)


def main():
    try:
        a = analyze()
    except Exception as e:
        print(f"❌ Errore analisi: {e}")
        sys.exit(1)

    report = build_report(a)
    print(report)

    if TG_TOKEN and TG_ID:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_ID, "text": report},
            timeout=10,
        )
        print(f"Telegram: {r.status_code} {r.text if r.status_code != 200 else ''}")
        if r.status_code != 200:
            sys.exit(1)
    else:
        print("⚠️ Telegram non configurato: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID vuoti")


if __name__ == "__main__":
    main()
