import os
import sys
import requests
from google import genai

GEMINI_KEY = os.getenv("GEMINI_API_KEY")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_ID = os.getenv("TELEGRAM_CHAT_ID")


def get_market():
    """Prezzo e range 24h da Kraken (non blocca i server GitHub)."""
    r = requests.get("https://api.kraken.com/0/public/Ticker?pair=SOLUSD", timeout=10)
    print(f"Kraken status: {r.status_code}")
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"Kraken error: {data['error']}")
    t = next(iter(data["result"].values()))
    return {
        "price": float(t["c"][0]),
        "high24": float(t["h"][1]),
        "low24": float(t["l"][1]),
    }


def get_price_fallback():
    """Fallback: Coinbase."""
    r = requests.get("https://api.coinbase.com/v2/prices/SOL-USD/spot", timeout=10)
    r.raise_for_status()
    p = float(r.json()["data"]["amount"])
    return {"price": p, "high24": p * 1.04, "low24": p * 0.96}


def main():
    if not GEMINI_KEY:
        print("❌ Manca GEMINI_API_KEY")
        sys.exit(1)

    try:
        m = get_market()
    except Exception as e:
        print(f"⚠️ Kraken fallito ({e}), provo Coinbase")
        try:
            m = get_price_fallback()
        except Exception as e2:
            print(f"❌ Nessun prezzo disponibile: {e2}")
            sys.exit(1)

    price = m["price"]
    # Range: max tra ±4% e il range 24h (con piccolo margine)
    lower = min(price * 0.96, m["low24"] * 0.99)
    upper = max(price * 1.04, m["high24"] * 1.01)
    # Passo di griglia ~0.25% circa, tra 35 e 50 griglie
    grids = max(35, min(50, round((upper - lower) / (price * 0.0025))))
    step_pct = ((upper - lower) / grids) / price * 100

    comment = "n/d"
    try:
        client = genai.Client(api_key=GEMINI_KEY)
        res = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=(
                f"SOL/USDT: prezzo {price:.2f}, minimo 24h {m['low24']:.2f}, "
                f"massimo 24h {m['high24']:.2f}. In UNA frase in italiano, "
                f"descrivi la volatilità/trend e se è adatto a un grid bot."
            ),
        )
        comment = res.text.strip()
    except Exception as e:
        print(f"⚠️ Gemini non disponibile: {e}")

    report = (
        "🤖 PIONEX GRID – SOL/USDT\n\n"
        f"💰 Prezzo: ${price:.2f}\n"
        f"⬇️ LOWER: {lower:.2f}\n"
        f"⬆️ UPPER: {upper:.2f}\n"
        f"🔢 GRIDS: {grids} (passo ~{step_pct:.2f}%)\n\n"
        f"📝 {comment}"
    )
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


if __name__ == "__main__":
    main()
