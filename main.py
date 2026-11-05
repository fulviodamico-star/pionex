import os
import requests
import google.generativeai as genai
import sys

# Recupero chiavi
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def get_sol_price():
    """Recupera il prezzo attuale di SOL da Pionex in modo robusto"""
    try:
        # Proviamo l'endpoint globale dei ticker
        url = "https://api.pionex.com/api/v1/market/tickers"
        response = requests.get(url, timeout=15).json()
        
        if 'data' in response and 'tickers' in response['data']:
            for ticker in response['data']['tickers']:
                if ticker['symbol'] == 'SOL_USDT':
                    return float(ticker['last'])
        
        # Se non lo trova nel loop, proviamo errore specifico
        print(f"⚠️ Simbolo SOL_USDT non trovato nella lista. Risposta API: {response}")
        return None
    except Exception as e:
        print(f"❌ Errore durante il recupero del prezzo: {e}")
        return None

def main():
    if not GEMINI_API_KEY:
        print("❌ ERRORE: Chiave GEMINI_API_KEY mancante nei Secrets!")
        sys.exit(1)

    print("🚀 Avvio analisi AI Pionex...")
    
    # 1. Recupero Prezzo
    price = get_sol_price()
    if not price:
        print("❌ Impossibile procedere senza il prezzo attuale.")
        sys.exit(1)
        
    print(f"💰 Prezzo attuale SOL: ${price}")

    # 2. Analisi Gemini
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        prompt = f"""
        Prezzo attuale di Solana (SOL): {price} USDT.
        Sei un esperto di trading. Calcola un range di scalping per un grid bot (investimento 50 EUR).
        Fornisci:
        LOWER: (prezzo attuale -4%)
        UPPER: (prezzo attuale +4%)
        GRIDS: (un numero tra 35 e 50)
        ANALISI: (una breve frase sulla tendenza attuale)
        Rispondi in italiano in modo sintetico.
        """
        
        response = model.generate_content(prompt)
        ai_decision = response.text
    except Exception as e:
        print(f"❌ Errore Gemini: {e}")
        sys.exit(1)

    # 3. Output e Telegram
    report = f"🤖 *AI PIONEX REPORT*\n\n💰 SOL: ${price}\n\n{ai_decision}"
    print(report)

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            t_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(t_url, json={
                "chat_id": TELEGRAM_CHAT_ID, 
                "text": report, 
                "parse_mode": "Markdown"
            }, timeout=10)
            print("✅ Inviato a Telegram!")
        except:
            print("⚠️ Errore invio Telegram.")

if __name__ == "__main__":
    main()
