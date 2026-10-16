#!/usr/bin/env python3
"""
Diagnosi gratuita: verifica dai server GitHub tutte le fonti esterne usate dagli agenti.
NON chiama mai la generazione di Claude: usa solo il conteggio token, che è gratuito,
per controllare chiave e nomi dei modelli.
"""
import json
import os
import sys
import time
from pathlib import Path

import requests
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analisi import UA, _leggi_rss  # noqa: E402
import analisi  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
risultati = []


def prova(nome, funzione):
    t0 = time.time()
    try:
        ok, dettaglio = funzione()
    except Exception as e:
        ok, dettaglio = False, f"errore: {e}"
    risultati.append((ok, nome, dettaglio, time.time() - t0))
    print(f"{'OK ' if ok else 'KO '} {nome}: {dettaglio} ({time.time() - t0:.1f}s)", flush=True)


def nasdaq():
    r = requests.get("https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true",
                     headers={**UA, "Accept": "application/json"}, timeout=40)
    r.raise_for_status()
    righe = (r.json().get("data") or {}).get("rows") or []
    return len(righe) > 1000, f"{len(righe)} azioni USA ricevute"


def download_misto():
    simboli = ["AAPL", "ENI.MI", "SAP.DE", "BTC-USD", "EURUSD=X", "GC=F", "^GSPC", "^VIX"]
    df = yf.download(simboli, period="1y", interval="1d", group_by="ticker", auto_adjust=True, progress=False)
    validi = [s for s in simboli if s in set(df.columns.get_level_values(0)) and df[s]["Close"].dropna().shape[0] > 100]
    mancanti = sorted(set(simboli) - set(validi))
    return len(validi) == len(simboli), f"{len(validi)}/{len(simboli)} validi" + (f", mancano {mancanti}" if mancanti else "")


def download_blocco():
    r = requests.get("https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true",
                     headers={**UA, "Accept": "application/json"}, timeout=40)
    righe = sorted(r.json()["data"]["rows"], key=lambda x: -float(str(x.get("marketCap") or 0).replace(",", "") or 0))
    simboli = [x["symbol"].replace("/", "-").replace(".", "-") for x in righe[:100]]
    t0 = time.time()
    df = yf.download(simboli, period="1y", interval="1d", group_by="ticker", auto_adjust=True, threads=True, progress=False)
    livello = set(df.columns.get_level_values(0))
    validi = sum(1 for s in simboli if s in livello and df[s]["Close"].dropna().shape[0] > 100)
    durata = time.time() - t0
    stima = durata * 22 / 60
    return validi >= 90, f"{validi}/100 validi in {durata:.0f}s (stima per ~2200 strumenti: {stima:.0f} minuti)"


def calendario():
    cal = yf.Ticker("AAPL").calendar
    d = cal.get("Earnings Date") if isinstance(cal, dict) else None
    return bool(d), f"prossima trimestrale AAPL: {d}"


def fondamentali():
    info = yf.Ticker("MSFT").info or {}
    campi = [k for k in ("sector", "trailingPE", "revenueGrowth", "profitMargins") if info.get(k) is not None]
    return len(campi) >= 3, f"campi presenti: {campi}"


def news_yfinance():
    n = yf.Ticker("NVDA").news or []
    return len(n) > 0, f"{len(n)} notizie"


def rss(nome, url, predef=""):
    def f():
        analisi._FONTI_KO.pop(nome, None)
        out = _leggi_rss(nome, url, 5, predef)
        return len(out) > 0, (f"{len(out)} notizie, es. «{out[0]['titolo'][:70]}»" if out else "nessuna notizia")
    return f


def claude_modelli():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False, "secret ANTHROPIC_API_KEY mancante"
    import anthropic
    client = anthropic.Anthropic()
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    b = cfg.get("breve", {})
    modelli = sorted({cfg.get("modello"), b.get("modello_analista"), b.get("modello_critico"), b.get("modello_retrospettiva")} - {None})
    esiti = []
    for m in modelli:
        try:
            # conteggio token: gratuito, verifica chiave e nome del modello
            client.messages.count_tokens(model=m, messages=[{"role": "user", "content": "prova"}])
            esiti.append(f"{m} ok")
        except Exception as e:
            esiti.append(f"{m} ERRORE {str(e)[:120]}")
    return all(e.endswith(" ok") for e in esiti), "; ".join(esiti)


def main():
    print("=== Diagnosi fonti (nessun costo API) ===", flush=True)
    prova("Screener Nasdaq (universo USA)", nasdaq)
    prova("Download Yahoo misto (azioni, crypto, forex, futures, indici)", download_misto)
    prova("Download Yahoo blocco da 100 azioni", download_blocco)
    prova("Date trimestrali (yfinance calendar)", calendario)
    prova("Fondamentali (yfinance info)", fondamentali)
    prova("Notizie yfinance", news_yfinance)
    prova("Notizie Yahoo RSS", rss("Yahoo RSS", "https://feeds.finance.yahoo.com/rss/2.0/headline?s=NVDA&region=US&lang=en-US", "Yahoo Finance"))
    prova("Notizie Bing RSS", rss("Bing News", "https://www.bing.com/news/search?q=Nvidia+stock&format=rss"))
    prova("Notizie Google RSS", rss("Google News", "https://news.google.com/rss/search?q=Nvidia+stock+when%3A7d&hl=en-US&gl=US&ceid=US%3Aen"))
    prova("Chiave e modelli Claude (conteggio token, gratuito)", claude_modelli)

    print("\n=== Riepilogo ===")
    for ok, nome, dett, _ in risultati:
        print(f"{'✅' if ok else '❌'} {nome}")
    notizie_ok = any(ok for ok, nome, _, _ in risultati if nome.startswith("Notizie"))
    essenziali = [r for r in risultati if not r[1].startswith("Notizie") and not r[1].startswith("Fondamentali")
                  and not r[1].startswith("Date")]
    pronto = all(ok for ok, *_ in essenziali) and notizie_ok
    print("\nPRONTO per lanciare l'agente breve." if pronto else
          "\nNON lanciare l'agente: incolla questo riepilogo in chat.")


if __name__ == "__main__":
    main()
