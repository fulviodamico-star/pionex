import os
import requests
import google.generativeai as genai
import sys

# Chiavi dai Secrets di GitHub
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def get_sol_price():
    """Tenta di recuperare il prezzo da 3 fonti diverse"""
    # 1. Prova Binance
    try:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT"
        resp = requests.get(url, timeout=10).json()
        if 'price' in resp:
            print("✅ Prezzo ottenuto da Binance")
            return float(resp['price'])
    except: pass

    # 2. Prova CryptoCompare (Pubblica)
    try:
        url = "https://min-api.cryptocompare.com/data/price?fsym=SOL&tsyms=USDT"
        resp = requests.get(url, timeout=10).json()
        if 'USDT' in resp:
            print("✅ Prezzo ottenuto da CryptoCompare")
            return float(resp['USDT'])
    except: pass

    # 3. Prova Pionex (con headers)
    try:
        url = "https://api.pionex.com/api/v1/market/tickers"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10).json()
        for t in resp['data']['tickers']:
            if t['symbol'] in ['SOL_USDT', 'SOLUSDT']:
                print("✅ Prezzo ottenuto da Pionex")
                return float(t['last'])
    except: pass

    return None

def main():
    if not GEMINI_API_KEY:
        print("❌ ERRORE: Chiave API Gemini mancante nei Secrets!")
        sys.exit(1)

    print("🚀 Avvio Analisi AI...")
    
    price = get_sol_price()
    if not price:
        print("❌ ERRORE: Impossibile recuperare il prezzo da nessuna fonte.")
        sys.exit(1)
        
    print(f"💰 Prezzo Solana: ${price}")

    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        prompt = f"""
        Prezzo SOL: {price} USDT.
        Agisci come esperto di trading. Dammi un range per Grid Bot scalping (investimento 50€).
        Rispondi esattamente in questo formato:
        LOWER: (prezzo -4%)
        UPPER: (prezzo +4%)
        GRIDS: (un numero tra 35 e 50)
        ANALISI: (una frase veloce sul trend)
        """
        
        response = model.generate_content(prompt)
        report = f"🤖 *AI PIONEX REPORT*\n\n💰 SOL: ${price}\n\n{response.text}"
        print(report)

        # Invio Telegram se configurato
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            t_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(t_url, json={"chat_id": TELEGRAM_CHAT_ID, "text": report, "parse_mode": "Markdown"})
            print("✅ Inviato a Telegram")

    except Exception as e:
        print(f"❌ Errore AI: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
