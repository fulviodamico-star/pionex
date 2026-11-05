import os
import requests
import google.generativeai as genai
import sys

# Recupero chiavi dai Secrets di GitHub
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def get_sol_price():
    """Recupera il prezzo di SOL con headers per evitare blocchi"""
    url = "https://api.pionex.com/api/v1/market/tickers"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    
    try:
        print("🌐 Connessione a Pionex API...")
        response = requests.get(url, headers=headers, timeout=20)
        
        if response.status_code != 200:
            print(f"❌ Errore HTTP {response.status_code}")
            return None
            
        data = response.json()
        
        # Cerchiamo SOL_USDT o SOLUSDT nella lista dei ticker
        if 'data' in data and 'tickers' in data['data']:
            for ticker in data['data']['tickers']:
                # Controlliamo entrambi i formati possibili
                if ticker.get('symbol') in ['SOL_USDT', 'SOLUSDT']:
                    price = float(ticker['last'])
                    return price
            
            print("⚠️ SOL_USDT non trovato nella lista dei ticker.")
            # Stampiamo i primi 3 ticker per debug
            print(f"Esempi di simboli trovati: {[t['symbol'] for t in data['data']['tickers'][:3]]}")
        else:
            print(f"❌ Struttura dati API inattesa: {data}")
            
    except Exception as e:
        print(f"❌ Errore durante il recupero del prezzo: {e}")
    
    return None

def main():
    if not GEMINI_API_KEY:
        print("❌ ERRORE: Chiave GEMINI_API_KEY non trovata nei Secrets di GitHub!")
        sys.exit(1)

    print("🚀 Avvio analisi AI Pionex...")
    
    # 1. Recupero Prezzo
    price = get_sol_price()
    if not price:
        print("❌ Impossibile ottenere il prezzo. Lo script termina qui.")
        sys.exit(1)
        
    print(f"✅ Prezzo attuale SOL: ${price}")

    # 2. Analisi Gemini
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        prompt = f"""
        Prezzo attuale SOL: {price} USDT.
        Analizza per Grid Bot scalping (investimento 50 EUR).
        Fornisci esattamente:
        LOWER: (prezzo -4%)
        UPPER: (prezzo +4%)
        GRIDS: (tra 35 e 48)
        ANALISI: (1 frase in italiano)
        """
        
        response = model.generate_content(prompt)
        ai_decision = response.text
        print("🧠 Analisi AI completata.")
    except Exception as e:
        print(f"❌ Errore Gemini: {e}")
        sys.exit(1)

    # 3. Report finale
    report = f"🤖 *AI PIONEX REPORT*\n\n💰 SOL: ${price}\n\n{ai_decision}"
    print("-" * 30)
    print(report)
    print("-" * 30)

    # 4. Invio Telegram
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            t_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(t_url, json={
                "chat_id": TELEGRAM_CHAT_ID, 
                "text": report, 
                "parse_mode": "Markdown"
            }, timeout=10)
            print("✅ Report inviato a Telegram!")
        except Exception as e:
            print(f"⚠️ Errore Telegram: {e}")

if __name__ == "__main__":
    main()
