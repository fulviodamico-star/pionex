import os
import requests
import google.generativeai as genai
import sys

# 1. Configurazione Chiavi
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_ID = os.getenv("TELEGRAM_CHAT_ID")

def get_price():
    """Recupera il prezzo da Binance (Standard Mondiale)"""
    try:
        url = "https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT"
        response = requests.get(url, timeout=10)
        data = response.json()
        if 'price' in data:
            return float(data['price'])
        else:
            print(f"⚠️ Risposta inattesa da Binance: {data}")
            return None
    except Exception as e:
        print(f"❌ Errore connessione Prezzo: {e}")
        return None

def main():
    print("🚀 Avvio Bot...")

    # Controllo API Key
    if not GEMINI_KEY:
        print("❌ ERRORE: Manca GEMINI_API_KEY nei Secrets di GitHub!")
        sys.exit(1)

    # Recupero Prezzo
    sol_price = get_price()
    if sol_price is None:
        print("❌ ERRORE: Impossibile ottenere il prezzo di Solana.")
        sys.exit(1)
    
    print(f"✅ Prezzo Solana trovato: ${sol_price}")

    # Analisi AI
    try:
        genai.configure(api_key=GEMINI_KEY)
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        prompt = f"""
        Prezzo attuale SOL: {sol_price} USDT.
        Analizza per Grid Bot scalping (investimento 50 EUR).
        Rispondi ESATTAMENTE così:
        LOWER: (prezzo -4%)
        UPPER: (prezzo +4%)
        GRIDS: (un numero tra 35 e 50)
        ANALISI: (una frase sul trend)
        """
        
        res = model.generate_content(prompt)
        report = f"🤖 *AI REPORT*\n\n💰 SOL: ${sol_price}\n\n{res.text}"
        
        print("-" * 20)
        print(report)
        print("-" * 20)

        # Telegram
        if TG_TOKEN and TG_ID:
            requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                          json={"chat_id": TG_ID, "text": report, "parse_mode": "Markdown"})
            print("✅ Inviato a Telegram!")

    except Exception as e:
        print(f"❌ Errore durante l'analisi AI: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
