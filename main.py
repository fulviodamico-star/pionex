name: Pionex Fast-Scalp AI Bot

on:
  # TRIGGER PRINCIPALE (affidabile): servizio esterno (cron-job.org) ogni 30 min -> repository_dispatch "tick"
  repository_dispatch:
    types: [tick]

  # BACKUP: il cron nativo di GitHub e' "best effort" (ritardi/salti): lo teniamo solo come rete di sicurezza
  schedule:
    - cron: '47 7-21 * * *'
    - cron: '17 22-23,0-6 * * *'

  # Esecuzione manuale / comandi d'emergenza dalla console HTML
  workflow_dispatch:
    inputs:
      command:
        description: 'status = solo report | close = chiudi posizione | sell_all = vendi tutto il saldo della coin | open = apri long'
        type: choice
        default: status
        options: [status, close, sell_all, open]
      coin:
        description: 'Coin (es. SOL, BTC, ETH) - per open / sell_all'
        type: string
        default: ''
      amount_usdt:
        description: 'Importo USDT per open (vuoto = automatico)'
        type: string
        default: ''

permissions:
  contents: write

# Mai due esecuzioni in parallelo: evita doppi acquisti / stato sovrascritto
concurrency:
  group: pionex-bot
  cancel-in-progress: false

jobs:
  run-bot:
    runs-on: ubuntu-latest
    timeout-minutes: 10

    steps:
      - name: Checkout repository
        uses: actions/checkout@v6

      - name: Setup Python 3.13
        uses: actions/setup-python@v6
        with:
          python-version: '3.13'

      - name: Install dependencies
        run: python -m pip install --upgrade pip "requests>=2.32.5,<3"

      - name: Run Pionex AI Fast-Scalp Engine
        env:
          TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN || secrets.TELEGRAM_BOT_TOKEN || secrets.BOT_TOKEN || secrets.TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID || secrets.CHAT_ID || secrets.TELEGRAM_CHAT }}
          PIONEX_API_KEY: ${{ secrets.PIONEX_API_KEY || secrets.API_KEY }}
          PIONEX_API_SECRET: ${{ secrets.PIONEX_API_SECRET || secrets.API_SECRET }}
          # KILL-SWITCH: trading reale SOLO se la variabile di repository AUTO_TRADE_ENABLED = true
          AUTO_TRADE_ENABLED: ${{ vars.AUTO_TRADE_ENABLED || 'false' }}
          INVEST_EUR: ${{ vars.INVEST_EUR || '50' }}
          GITHUB_EVENT_NAME: ${{ github.event_name }}
          MANUAL_COMMAND: ${{ github.event.inputs.command || '' }}
          MANUAL_COIN: ${{ github.event.inputs.coin || '' }}
          MANUAL_USDT: ${{ github.event.inputs.amount_usdt || '' }}
        run: python main.py

      # always(): lo stato viene salvato anche se il bot esce con errore dopo aver mandato un ordine reale
      - name: Save Bot State & Live Console (Auto-Commit)
        if: always()
        run: |
          git config --local user.email "github-actions[bot]@users.noreply.github.com"
          git config --local user.name "github-actions[bot]"
          git add bot_state.json index.html
          git diff --staged --quiet || (git commit -m "chore: update fast-scalp state and live console" && git pull --rebase -X theirs origin ${{ github.ref_name }} && git push)
