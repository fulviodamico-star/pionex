#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Console Web in tempo reale per multi_coin_ai_bot_v2.py  (solo libreria standard, nessuna dipendenza extra)

Avvio:   python console_server.py        ->  http://127.0.0.1:8080
Env:     CONSOLE_TOKEN      token d'accesso (se assente ne viene generato uno e stampato all'avvio)
         CONSOLE_HOST       default 127.0.0.1 (per accesso remoto usa tunnel SSH / Tailscale / reverse proxy HTTPS)
         CONSOLE_PORT       default 8080
         SCAN_SECONDS       ogni quanti secondi rifare l'analisi completa (default 180)
         PRICE_SECONDS      ogni quanti secondi aggiornare il prezzo della posizione aperta (default 5)
         CONSOLE_MANAGE     true/false: la console controlla stop/target/trailing in tempo reale (default true)
         CONSOLE_AUTOPILOT  true/false: la console può anche APRIRE posizioni da sola (default false)

ATTENZIONE: usa la console OPPURE il cron del bot, non entrambi, perché condividono bot_state.json.
"""
import os
import re
import json
import time
import hmac
import html
import secrets
import threading
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import multi_coin_ai_bot_v2 as b

HOST = os.getenv("CONSOLE_HOST") or "127.0.0.1"
PORT = int(os.getenv("CONSOLE_PORT") or 8080)
SCAN_SECONDS = int(os.getenv("SCAN_SECONDS") or 180)
PRICE_SECONDS = int(os.getenv("PRICE_SECONDS") or 5)
MANAGE = (os.getenv("CONSOLE_MANAGE") or "true").lower() == "true"
AUTOPILOT = (os.getenv("CONSOLE_AUTOPILOT") or "false").lower() == "true"
TOKEN = os.getenv("CONSOLE_TOKEN") or secrets.token_urlsafe(18)
MAX_ANALYSIS_AGE = 600   # secondi: oltre questa età non si apre una posizione su dati vecchi

LOCK = threading.RLock()                 # protegge bot_state.json
SCAN = {"ts": 0.0, "by_coin": {}, "order": [], "news": None, "error": None, "scanning": False}
LIVE = {"coin": None, "price": 0.0, "ts": 0.0}
EVENTS = deque(maxlen=150)
SCAN_NOW = threading.Event()


class ApiError(Exception):
    pass


def plain(s):
    return html.unescape(re.sub(r"<[^>]+>", "", str(s)))


def log_event(msg, notify=False):
    now = datetime.now(ZoneInfo("Europe/Rome")).strftime("%d/%m %H:%M:%S")
    EVENTS.appendleft({"t": now, "msg": plain(msg).replace("\n", " ")})
    if notify:
        try:
            b.send_telegram(msg)
        except Exception as e:
            print("Telegram:", e)


def now_str():
    return datetime.now(ZoneInfo("Europe/Rome")).strftime("%d/%m %H:%M")


# ----------------------------------------------------------------------------
# Analisi in background
# ----------------------------------------------------------------------------

def run_scan():
    SCAN["scanning"] = True
    try:
        try:
            news = b.fetch_macro_calendar()
        except Exception as e:
            news = b.empty_news(datetime.now(ZoneInfo("Europe/Rome")), str(e))
        best = b.scan_all_coins(news)
        coins = best["all_coins"]
        if AUTOPILOT:
            with LOCK:
                b.execute_autonomous_cycle(best, news)
            for line in best.get("auto_log", []):
                if line.startswith(("🚀", "🔔", "❌")):
                    log_event(line, notify=True)
        SCAN.update({"ts": time.time(), "by_coin": {c["coin"]: c for c in coins},
                     "order": [c["coin"] for c in coins], "news": news, "error": None})
    except Exception as e:
        SCAN["error"] = f"{type(e).__name__}: {e}"
        log_event(f"⚠️ Scansione fallita: {e}")
    finally:
        SCAN["scanning"] = False


def scan_loop():
    while True:
        run_scan()
        SCAN_NOW.wait(SCAN_SECONDS)
        SCAN_NOW.clear()


def analysis_age():
    return time.time() - SCAN["ts"] if SCAN["ts"] else 1e9


def price_loop():
    while True:
        try:
            with LOCK:
                st = b.load_bot_state()
            if st.get("active"):
                coin = st["coin"]
                price = b.fetch_last_price(b.COIN_BY_SYMBOL[coin]["kraken"])
                LIVE.update({"coin": coin, "price": price, "ts": time.time()})
                if MANAGE:
                    a = SCAN["by_coin"].get(coin) if analysis_age() < MAX_ANALYSIS_AGE else None
                    with LOCK:
                        st = b.load_bot_state()
                        if st.get("active") and st["coin"] == coin:
                            closed, lines = b.manage_open_position(
                                st, price, a["atr_abs"] if a else 0.0,
                                a["ai_prob"] if a else None, a["final_score"] if a else None, now_str())
                            b.save_bot_state(st)
                            for ln in lines:
                                if ln.startswith(("🔔", "❌")):
                                    log_event(ln, notify=True)
        except Exception as e:
            log_event(f"⚠️ Aggiornamento prezzo: {e}")
        time.sleep(PRICE_SECONDS)


# ----------------------------------------------------------------------------
# Azioni manuali
# ----------------------------------------------------------------------------

def open_position(coin, usdt=None, force=False):
    coin = (coin or "").upper()
    with LOCK:
        st = b.load_bot_state()
        if st.get("active"):
            raise ApiError(f"C'è già una posizione aperta su {st['coin']}: chiudila prima.")
        a = SCAN["by_coin"].get(coin)
        if not a:
            raise ApiError("Nessuna analisi disponibile per questa moneta.")
        if analysis_age() > MAX_ANALYSIS_AGE:
            raise ApiError("Analisi troppo vecchia: premi «Aggiorna analisi» e riprova.")
        rk = a["risk_check"]
        if not rk["passed"] and not force:
            raise ApiError("Risk Manager BLOCK: " + plain(rk["reason"]))
        bo = rk["bracket_order"]
        notional = float(usdt) if usdt else bo["order_usdt"]
        notional = min(notional, a["invest_usdt"] * (1 - b.CASH_BUFFER_PCT / 100.0))
        if notional < b.MIN_POSITION_USDT:
            raise ApiError(f"Size {notional:.1f}$ sotto il minimo ({b.MIN_POSITION_USDT:.0f}$).")
        cfg = b.COIN_BY_SYMBOL[coin]
        price = b.fetch_last_price(cfg["kraken"])
        stop = a["stop_loss"]
        if price <= stop or price >= a["upper"]:
            raise ApiError("Il prezzo attuale è fuori dal piano (sotto lo stop o sopra il target): rianalizza.")
        qty = round(notional / price, 6)
        ok, note = b.execute_order("buy", cfg["pionex"], price, qty, amount_usdt=notional)
        if not ok:
            raise ApiError("Ordine rifiutato: " + plain(note))
        ns = now_str()
        st.update({
            "active": True, "coin": coin, "pionex_symbol": cfg["pionex"], "entry_price": price,
            "highest_price": price, "last_price": price, "lower": a["lower"], "upper": a["upper"],
            "stop": stop, "initial_stop": stop, "one_r_usd": price - stop, "grids": a["grids"],
            "qty_coin": qty, "notional_usdt": round(notional, 2), "atr_abs": a["atr_abs"],
            "trailing_locked": False, "alert_1r_sent": False, "alert_2r_sent": False,
            "pending_close_fail": 0, "opened_at": ns, "opened_date": ns[:5],
            "last_action": f"[{ns}] APERTURA MANUALE {coin}/USDT a ${b.fmt_p(price)}" + (" (FORZATA)" if force and not rk["passed"] else "")})
        b.save_bot_state(st)
    msg = (f"🖐️ <b>APERTURA MANUALE {coin}/USDT</b>{' (forzata oltre il Risk Manager)' if force and not rk['passed'] else ''}\n"
           f"Entry ${b.fmt_p(price)} | Stop ${b.fmt_p(stop)} | Target ${b.fmt_p(a['upper'])} | Size {notional:.1f}$\n{html.escape(note)}")
    log_event(msg, notify=True)
    return {"ok": True, "note": note}


def close_position(reason="CHIUSURA MANUALE"):
    with LOCK:
        st = b.load_bot_state()
        if not st.get("active"):
            raise ApiError("Nessuna posizione aperta.")
        coin = st["coin"]
        price = b.fetch_last_price(b.COIN_BY_SYMBOL[coin]["kraken"])
        ok, note = b.execute_order("sell", st.get("pionex_symbol", f"{coin}_USDT"), price, st.get("qty_coin", 0.0))
        if not ok:
            raise ApiError("Vendita rifiutata: " + plain(note) + " — controlla Pionex a mano!")
        entry, one_r = st["entry_price"], st.get("one_r_usd") or st["entry_price"] * 0.02
        pnl_pct = (price - entry) / entry * 100.0
        pnl_usdt = (st.get("notional_usdt") or 0.0) * (pnl_pct - b.COST_PCT) / 100.0
        r = (price - entry) / one_r
        hist = st.get("history", [])
        hist.insert(0, {"coin": coin, "opened_at": st.get("opened_at", "-"), "closed_at": now_str(),
                        "entry_price": entry, "exit_price": price, "pnl_pct": round(pnl_pct - b.COST_PCT, 2),
                        "pnl_usdt": round(pnl_usdt, 2), "r_multiple": round(r, 2), "reason": "🖐️ " + reason})
        st["history"] = hist[:60]
        st["active"] = False
        st["pending_close_fail"] = 0
        st["last_action"] = f"[{now_str()}] {reason} {coin} a ${b.fmt_p(price)} ({pnl_pct:+.2f}% / {r:+.1f}R)"
        b.save_bot_state(st)
    log_event(f"🖐️ <b>CHIUSURA MANUALE {coin}/USDT</b>: ${b.fmt_p(entry)} → ${b.fmt_p(price)} "
              f"({pnl_pct - b.COST_PCT:+.2f}% | {pnl_usdt:+.2f}$ | {r:+.2f}R). {html.escape(note)}", notify=True)
    return {"ok": True, "note": note}


# ----------------------------------------------------------------------------
# Suggerimento operativo + snapshot
# ----------------------------------------------------------------------------

def compute_advice(st):
    fresh = analysis_age() < MAX_ANALYSIS_AGE
    if st.get("active"):
        coin = st["coin"]
        a = SCAN["by_coin"].get(coin) if fresh else None
        price = LIVE["price"] if LIVE["coin"] == coin and time.time() - LIVE["ts"] < 60 else st.get("last_price") or st["entry_price"]
        if price <= st["stop"] * 1.004:
            return {"action": "CLOSE", "coin": coin, "text": f"Prezzo a ridosso dello stop (${b.fmt_p(st['stop'])}): chiudi o lascia scattare lo stop."}
        if a and a["final_score"] <= 3:
            return {"action": "CLOSE", "coin": coin, "text": f"Il voto di {coin} è sceso a {a['final_score']}/10: valuta di chiudere."}
        if a and a["ai_prob"] < 0.40 and a["final_score"] <= 4:
            return {"action": "CLOSE", "coin": coin, "text": f"AI sfavorevole ({a['ai_prob']*100:.0f}%) e voto {a['final_score']}/10: valuta di chiudere."}
        if price >= st["upper"] * 0.995:
            return {"action": "CLOSE", "coin": coin, "text": "Target quasi raggiunto: valuta di incassare."}
        return {"action": "HOLD", "coin": coin, "text": f"Mantieni: nessun segnale di uscita (voto {a['final_score']}/10)." if a else "Mantieni: nessun segnale di uscita (analisi non fresca)."}
    if not fresh or not SCAN["order"]:
        return {"action": "WAIT", "coin": None, "text": "Analisi in corso o non aggiornata."}
    w = SCAN["by_coin"][SCAN["order"][0]]
    rk, et = w["risk_check"], w["entry_timer"]
    if w["final_score"] >= 7 and w["ai_prob"] >= 0.52 and rk["passed"] and et["triggered"]:
        return {"action": "OPEN", "coin": w["coin"], "text": f"Segnale su {w['coin']}/USDT: voto {w['final_score']}/10, {et['setup_name']}, E[R] {w['time_machine']['expected_r']:+.2f}R."}
    why = plain(rk["reason"]) if not rk["passed"] else (et["status_txt"] if not et["triggered"] else f"voto {w['final_score']}/10 o AI {w['ai_prob']*100:.0f}% sotto soglia")
    return {"action": "WAIT", "coin": w["coin"], "text": f"Attendi. Migliore: {w['coin']} ({w['final_score']}/10). Motivo: {why}"}


def coin_view(a):
    tm, rk, ep, et, bo = a["time_machine"], a["risk_check"], a["exit_plan"], a["entry_timer"], a["risk_check"]["bracket_order"]
    return {
        "coin": a["coin"], "price": b.fmt_p(a["price"]), "chg24": a["chg24"], "score": a["final_score"],
        "pts": a["pts_100"], "ai": a["ai_prob"], "er": tm["expected_r"], "win": tm["win_or_grid_prob"],
        "stopfirst": tm["stop_first_prob"], "rr": ep["rr_ratio"], "risk_ok": rk["passed"],
        "risk_reason": plain(rk["reason"]), "setup": et["setup_name"], "triggered": et["triggered"],
        "setup_txt": plain(et["status_txt"]), "lower": b.fmt_p(a["lower"]), "upper": b.fmt_p(a["upper"]),
        "stop": b.fmt_p(a["stop_loss"]), "grids": a["grids"], "size": bo["order_usdt"], "risk_usdt": rk["risk_usdt"],
        "max_loss": rk["max_loss_usdt"], "breakdown": [plain(x) for x in a["breakdown"]],
        "veto": [plain(x) for x in a["veto_reasons"]],
    }


def snapshot():
    with LOCK:
        st = b.load_bot_state()
    pos = None
    if st.get("active"):
        coin = st["coin"]
        price = LIVE["price"] if LIVE["coin"] == coin and LIVE["price"] else st.get("last_price") or st["entry_price"]
        entry, one_r = st["entry_price"], st.get("one_r_usd") or st["entry_price"] * 0.02
        pnl_pct = (price - entry) / entry * 100.0
        pos = {"coin": coin, "entry": b.fmt_p(entry), "price": b.fmt_p(price), "stop": b.fmt_p(st["stop"]),
               "lower": b.fmt_p(st["lower"]), "upper": b.fmt_p(st["upper"]), "opened_at": st.get("opened_at"),
               "pnl_pct": pnl_pct, "pnl_usdt": (st.get("notional_usdt") or 0) * (pnl_pct - b.COST_PCT) / 100.0,
               "r": (price - entry) / one_r, "size": st.get("notional_usdt"), "qty": st.get("qty_coin"),
               "locked": st.get("trailing_locked"), "fail": st.get("pending_close_fail", 0),
               "range_pct": max(0, min(100, (price - st["lower"]) / max(st["upper"] - st["lower"], 1e-9) * 100)),
               "price_age": round(time.time() - LIVE["ts"], 1) if LIVE["ts"] else None}
    news, nv = SCAN["news"], None
    if news:
        ne = news.get("next_event")
        nv = {"status": news["status"], "us_open": news["us_open_warning"],
              "imminent": [e["title"] for e in news["imminent"]],
              "next": f"{ne['title']} ({ne['date_str']} {ne['time_str']})" if ne else None}
    best = SCAN["by_coin"].get(SCAN["order"][0]) if SCAN["order"] else None
    return {"live": b.is_live(), "autopilot": AUTOPILOT, "manage": MANAGE, "scan_age": round(analysis_age()) if SCAN["ts"] else None,
            "scanning": SCAN["scanning"], "scan_error": SCAN["error"], "scan_seconds": SCAN_SECONDS,
            "position": pos, "advice": compute_advice(st), "coins": [coin_view(SCAN["by_coin"][c]) for c in SCAN["order"]],
            "history": st.get("history", []), "news": nv, "events": list(EVENTS),
            "btc": {"chg24": best["btc"]["chg24h"]} if best else None, "fng": best["fng"]["value"] if best else None,
            "last_action": st.get("last_action")}


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        body = payload.encode() if isinstance(payload, str) else json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        return hmac.compare_digest(self.headers.get("X-Token", ""), TOKEN)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if self.path == "/api/snapshot":
            if not self._authed():
                return self._send(401, {"error": "token non valido"})
            try:
                return self._send(200, snapshot())
            except Exception as e:
                return self._send(500, {"error": str(e)})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"error": "token non valido"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/api/open":
                return self._send(200, open_position(data.get("coin"), data.get("usdt"), bool(data.get("force"))))
            if self.path == "/api/close":
                return self._send(200, close_position())
            if self.path == "/api/scan":
                SCAN_NOW.set()
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})
        except ApiError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


PAGE = r"""<!DOCTYPE html>
<html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Quant Bot Console</title>
<style>
:root{--bg:#0b1120;--card:#111a2e;--line:#1f2b45;--tx:#e5eaf5;--mut:#8a98b8;--g:#34d399;--r:#fb7185;--y:#fbbf24;--b:#38bdf8}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px system-ui,sans-serif;padding:14px}
.wrap{max-width:1200px;margin:auto;display:grid;gap:14px}.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}
h1{font-size:18px;margin:0}h3{margin:0 0 10px;font-size:14px}.row{display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between}
.pill{padding:3px 10px;border-radius:99px;font-size:12px;font-weight:700;border:1px solid var(--line)}
.g{color:var(--g)}.r{color:var(--r)}.y{color:var(--y)}.b{color:var(--b)}.m{color:var(--mut)}
.banner{border-radius:14px;padding:16px;font-size:16px;font-weight:700;border:1px solid}
.OPEN{background:#064e3b55;border-color:#10b981}.HOLD{background:#0c4a6e55;border-color:#0ea5e9}.WAIT{background:#78350f44;border-color:#f59e0b}.CLOSE{background:#7f1d1d66;border-color:#f43f5e}
.grid4{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.box{background:#0b1426;border-radius:10px;padding:10px}.box b{display:block;font-size:18px;margin-top:3px}
table{width:100%;border-collapse:collapse}th,td{padding:8px 6px;text-align:left;border-bottom:1px solid var(--line);font-size:13px;white-space:nowrap}th{color:var(--mut);font-weight:600}
tr.sel{background:#16233e}tbody tr{cursor:pointer}.sc{padding:2px 9px;border-radius:99px;border:1px solid;font-weight:700}
button{border:0;border-radius:9px;padding:8px 14px;font-weight:700;cursor:pointer;color:#06101f}button:disabled{opacity:.35;cursor:not-allowed}
.bg{background:var(--g)}.br{background:var(--r)}.bb{background:var(--b)}.bm{background:#334155;color:var(--tx)}
.bar{height:10px;background:#0b1426;border:1px solid var(--line);border-radius:99px;overflow:hidden}.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--b),var(--g))}
.scroll{overflow-x:auto}ul{margin:0;padding-left:18px}li{margin:4px 0}.ev{max-height:220px;overflow:auto;font-size:12px}.ev div{padding:3px 0;border-bottom:1px solid var(--line)}
</style></head><body><div class="wrap">
<div class="card row"><div><h1>🤖 Quant Bot Console</h1><div class="m" id="sub">connessione…</div></div>
<div class="row" style="gap:8px"><span class="pill" id="mode"></span><span class="pill" id="conn"></span><button class="bm" id="scanBtn">↻ Aggiorna analisi</button></div></div>
<div class="banner" id="advice"></div>
<div class="card" id="posCard"></div>
<div class="card"><h3>🏆 Classifica live (clic su una riga per il dettaglio)</h3><div class="scroll"><table><thead><tr><th>Moneta</th><th>Prezzo</th><th>24h</th><th>Voto</th><th>AI</th><th>E[R]</th><th>R:R</th><th>Risk</th><th>Setup</th><th></th></tr></thead><tbody id="rank"></tbody></table></div></div>
<div class="card" id="detail"></div>
<div class="card"><h3>📜 Posizioni chiuse</h3><div class="grid4" id="stats" style="margin-bottom:12px"></div><div class="scroll"><table><thead><tr><th>Moneta</th><th>Periodo</th><th>In → Out</th><th>Risultato</th><th>Motivo</th></tr></thead><tbody id="hist"></tbody></table></div></div>
<div class="row" style="align-items:stretch"><div class="card" style="flex:1;min-width:280px"><h3>📰 Macro & mercato</h3><div id="news"></div></div>
<div class="card" style="flex:2;min-width:300px"><h3>🔔 Eventi</h3><div class="ev" id="events"></div></div></div>
</div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let TOKEN=sessionStorage.getItem('tok')||'',SEL=null,S=null;
async function api(path,body){
  if(!TOKEN){TOKEN=prompt('Token della console:')||'';sessionStorage.setItem('tok',TOKEN);}
  const r=await fetch(path,{method:body===undefined?'GET':'POST',headers:{'X-Token':TOKEN,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  if(r.status===401){sessionStorage.removeItem('tok');TOKEN='';throw new Error('Token non valido');}
  const j=await r.json();if(!r.ok)throw new Error(j.error||r.status);return j;
}
const col=v=>v>=0?'g':'r',sgn=(v,d=2)=>(v>=0?'+':'')+Number(v).toFixed(d);
function scCol(s){return s>=7?'var(--g)':s>=5?'var(--y)':'var(--r)'}
async function poll(){
  try{S=await api('/api/snapshot');$('conn').textContent='● online';$('conn').className='pill g';render(S);}
  catch(e){$('conn').textContent='● offline: '+e.message;$('conn').className='pill r';}
}
function render(s){
  $('mode').textContent=s.live?'LIVE PIONEX':'PAPER TRADING';$('mode').className='pill '+(s.live?'g':'b');
  $('sub').textContent=`Analisi ${s.scanning?'in corso…':(s.scan_age==null?'in attesa':s.scan_age+'s fa')} (ogni ${s.scan_seconds}s) · gestione stop live: ${s.manage?'ON':'OFF'} · autopilot: ${s.autopilot?'ON':'OFF'}`+(s.scan_error?' · ⚠️ '+s.scan_error:'');
  const a=s.advice,ic={OPEN:'🟢 APRI',HOLD:'🔵 TIENI',WAIT:'🟡 ATTENDI',CLOSE:'🔴 CHIUDI'}[a.action];
  $('advice').className='banner '+a.action;$('advice').innerHTML=`${ic}${a.coin?' · '+esc(a.coin)+'/USDT':''}<div class="m" style="font-weight:400;font-size:13px;margin-top:4px">${esc(a.text)}</div>`;
  const p=s.position;
  if(p){
    $('posCard').innerHTML=`<div class="row"><h3>🟢 Posizione aperta: ${esc(p.coin)}/USDT <span class="m" style="font-weight:400">dal ${esc(p.opened_at)} · size ${Number(p.size).toFixed(1)}$ · prezzo ${p.price_age==null?'n/d':p.price_age+'s fa'}</span></h3>
    <button class="br" onclick="closePos()">Chiudi ora</button></div>
    ${p.fail?`<div class="r" style="margin:6px 0">⚠️ ${p.fail} tentativi di chiusura falliti: controlla Pionex a mano!</div>`:''}
    <div class="grid4" style="margin:10px 0"><div class="box"><span class="m">Entrata</span><b>$${p.entry}</b></div><div class="box"><span class="m">Prezzo</span><b class="b">$${p.price}</b></div>
    <div class="box"><span class="m">P&L</span><b class="${col(p.pnl_pct)}">${sgn(p.pnl_pct)}% (${sgn(p.pnl_usdt)}$)</b></div><div class="box"><span class="m">Multiplo R</span><b class="${col(p.r)}">${sgn(p.r)}R</b></div>
    <div class="box"><span class="m">Stop dinamico ${p.locked?'🔒':''}</span><b class="y">$${p.stop}</b></div><div class="box"><span class="m">Target</span><b>$${p.upper}</b></div></div>
    <div class="row m" style="font-size:12px;margin-bottom:4px"><span>$${p.lower}</span><span>posizione nel range ${p.range_pct.toFixed(0)}%</span><span>$${p.upper}</span></div><div class="bar"><i style="width:${p.range_pct}%"></i></div>`;
  }else $('posCard').innerHTML=`<h3>⚪ Nessuna posizione aperta</h3><div class="m">Ultima azione: ${esc(s.last_action)}</div>`;
  $('rank').innerHTML=s.coins.map((c,i)=>`<tr class="${c.coin===SEL?'sel':''}" onclick="SEL='${c.coin}';render(S)">
    <td><b>${i==0?'👑 ':''}${c.coin}/USDT</b></td><td>$${c.price}</td><td class="${col(c.chg24)}">${sgn(c.chg24,1)}%</td>
    <td><span class="sc" style="color:${scCol(c.score)};border-color:${scCol(c.score)}">${c.score}/10</span></td><td class="b">${(c.ai*100).toFixed(0)}%</td>
    <td class="${col(c.er)}">${sgn(c.er)}R</td><td>${c.rr.toFixed(2)}</td><td>${c.risk_ok?'✅':'⛔'}</td><td class="m">${esc(c.setup)} ${c.triggered?'✅':'⏳'}</td>
    <td><button class="bg" ${p?'disabled':''} onclick="event.stopPropagation();openPos('${c.coin}')">Apri</button></td></tr>`).join('')||'<tr><td colspan="10" class="m">Analisi in corso…</td></tr>';
  const d=s.coins.find(c=>c.coin===(SEL||(s.coins[0]||{}).coin));
  $('detail').innerHTML=d?`<h3>📊 Dettaglio ${d.coin}/USDT — piano: entry ~$${d.price} · range $${d.lower}–$${d.upper} (${d.grids} griglie) · stop $${d.stop} · size ${d.size}$ · rischio ${d.risk_usdt}$ (max ${d.max_loss}$)</h3>
    ${d.veto.length?`<ul class="r" style="margin-bottom:8px">${d.veto.map(v=>`<li>${esc(v)}</li>`).join('')}</ul>`:''}
    ${!d.risk_ok?`<div class="y" style="margin-bottom:8px">Risk Manager: ${esc(d.risk_reason)}</div>`:''}<ul>${d.breakdown.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:'';
  const h=s.history,n=h.length,w=h.filter(x=>x.pnl_usdt>0).length,tot=h.reduce((a,x)=>a+x.pnl_usdt,0),tr=h.reduce((a,x)=>a+(x.r_multiple||0),0);
  $('stats').innerHTML=`<div class="box"><span class="m">Trade chiusi</span><b>${n}</b></div><div class="box"><span class="m">Win rate</span><b>${n?(w/n*100).toFixed(0):'–'}%</b></div>
    <div class="box"><span class="m">P&L netto</span><b class="${col(tot)}">${sgn(tot)}$</b></div><div class="box"><span class="m">R totali</span><b class="${col(tr)}">${sgn(tr)}R</b></div>`;
  $('hist').innerHTML=h.map(x=>`<tr><td><b>${esc(x.coin)}/USDT</b></td><td>${esc(x.opened_at)} → ${esc(x.closed_at)}</td><td>$${x.entry_price} → $${x.exit_price}</td>
    <td class="${col(x.pnl_usdt)}"><b>${sgn(x.pnl_pct)}% (${sgn(x.pnl_usdt)}$ · ${sgn(x.r_multiple||0,1)}R)</b></td><td class="m">${esc(x.reason)}</td></tr>`).join('')||'<tr><td colspan="5" class="m">Nessun trade chiuso.</td></tr>';
  const nw=s.news;$('news').innerHTML=(nw?`<div>Stato: <b>${esc(nw.status)}</b>${nw.us_open?' · 🔔 NYSE Opening Bell':''}</div>${nw.imminent.map(e=>`<div class="r">🚨 ${esc(e)}</div>`).join('')}<div class="m">Prossimo: ${esc(nw.next||'nessuno')}</div>`:'<div class="m">n/d</div>')
    +`<div class="m" style="margin-top:8px">BTC 24h: ${s.btc?sgn(s.btc.chg24,1)+'%':'n/d'} · Fear&amp;Greed: ${s.fng??'n/d'}</div>`;
  $('events').innerHTML=s.events.map(e=>`<div><span class="m">${esc(e.t)}</span> ${esc(e.msg)}</div>`).join('')||'<div class="m">Nessun evento.</div>';
}
async function openPos(coin){
  const c=S.coins.find(x=>x.coin===coin);let force=false;
  if(!confirm(`Aprire ${coin}/USDT?\nEntry ~$${c.price} · Stop $${c.stop} · Target $${c.upper}\nSize ${c.size}$ · rischio a stop ${c.risk_usdt}$\n${S.live?'⚠️ ORDINE REALE su Pionex':'(paper trading)'}`))return;
  if(!c.risk_ok){if(!confirm(`⛔ Il Risk Manager BLOCCA questo trade:\n${c.risk_reason}\n\nVuoi FORZARE l'apertura comunque?`))return;force=true;}
  try{const r=await api('/api/open',{coin,force});alert('Fatto: '+r.note);}catch(e){alert('Errore: '+e.message);}poll();
}
async function closePos(){
  if(!confirm(`Chiudere ORA ${S.position.coin}/USDT a mercato?\n${S.live?'⚠️ ORDINE REALE su Pionex':'(paper trading)'}`))return;
  try{const r=await api('/api/close',{});alert('Fatto: '+r.note);}catch(e){alert('Errore: '+e.message);}poll();
}
$('scanBtn').onclick=async()=>{try{await api('/api/scan',{});}catch(e){alert(e.message)}setTimeout(poll,1500);};
poll();setInterval(poll,3000);
</script></body></html>"""


def main():
    print(f"\n🖥️  Console: http://{HOST}:{PORT}\n🔑 Token: {TOKEN}\n"
          f"   Modalità: {'LIVE' if b.is_live() else 'PAPER'} | gestione stop live: {MANAGE} | autopilot: {AUTOPILOT}\n")
    threading.Thread(target=scan_loop, daemon=True).start()
    threading.Thread(target=price_loop, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
