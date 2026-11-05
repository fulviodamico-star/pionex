import os
import requests
import google.generativeai as genai

# ============================================
# RECUPERO CHIAVI DAI SECRETS DI GITHUB
# ============================================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # Opzionale
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")      # Opzionale

def send_telegram_message(message):
    """Invia il report direttamente sul tuo Telegram"""
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown"
            }
            requests.post(url, json=payload, timeout=10)
            print("✅ Messaggio Telegram inviato con successo!")
        except Exception as e:
            print(f"⚠️ Errore invio Telegram: {e}")

def get_sol_price():
    """Recupera il prezzo attuale di SOL da Pionex"""
    try:
        url = "https://api.pionex.com/api/v1/market/tickers?symbol=SOL_USDT"
        response = requests.get(url, timeout=10).json()
        price = float(response['data']['tickers'][0]['last'])
        return price
    except Exception as e:
        print(f"❌ Errore recupero prezzo: {e}")
        return None

def ask_gemini_for_range(current_price):
    """Chiede a Google Gemini di calcolare i parametri per lo scalping"""
    genai.configure(api_key=GEMINI_API_KEY)
    
    prompt = f"""
    Sei un esperto di trading algoritmico e scalping grid bot.
    Il prezzo attuale di Solana (SOL) è {current_price} USDT.
    
    Calcola il miglior range per uno Scalping Grid Bot da 50 EUR ad alta frequenza.
    Requisiti:
    - Range stretto per catturare tante oscillazioni.
    - Limite inferiore: circa -4% / -5% dal prezzo attuale.
    - Limite superiore: circa +4% / +5% dal prezzo attuale.
    - Numero griglie consigliato: tra 35 e 45.

    Rispondi ESCLUSIVAMENTE con questo formato esatto:
    LOWER: <valore_numerico>
    UPPER: <valore_numerico>
    GRIDS: <numero_griglie>
    ANALISI: <breve spiegazione di 2 righe sul mercato attuale in italiano>
    """
    
    try:
        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        print(f"❌ Errore Gemini: {e}")
        return None

def main():
    print("🚀 Avvio analisi AI Pionex...")
    
    price = get_sol_price()
    if not price:
        return
        
    ai_decision = ask_gemini_for_range(price)
    if not ai_decision:
        return

    # Formattazione del report
    report = f"🤖 *PIONEX AI SCALPER REPORT*\n\n"
    report += f"💰 *Prezzo attuale SOL:* ${price}\n"
    report += f"--------------------------------\n"
    report += f"{ai_decision}\n"
    report += f"--------------------------------\n"
    report += f"👉 *Azione:* Apri Pionex e aggiorna i parametri del bot se il prezzo e' uscito dal range."

    print(report)
    send_telegram_message(report)

if __name__ == "__main__":
    main()
