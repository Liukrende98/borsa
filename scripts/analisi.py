#!/usr/bin/env python3
"""
Analisi borsa continua.

1. Scarica i dati dei titoli in config.json (Yahoo Finance via yfinance)
2. Calcola indicatori e segnali in modo deterministico (nessun numero dall'AI)
3. Chiede a Claude un commento basato SOLO su quei dati
4. Salva docs/data.json (stato attuale) e docs/storico.json (storico analisi)
"""
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import costi  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.json"
OUT_DIR = ROOT / "docs"
DATA = OUT_DIR / "data.json"
STORICO = OUT_DIR / "storico.json"


# ----------------------------------------------------------------- utilità

def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def num(x, nd=4):
    """Float pulito per JSON (None se NaN/inf/non numerico)."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return round(x, nd)


def carica_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def salva_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def ora_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------ dati mercato

def scarica(simbolo, tentativi=3):
    t = yf.Ticker(simbolo)
    for i in range(tentativi):
        try:
            daily = t.history(period="1y", interval="1d", auto_adjust=True)
            if daily is None or daily.empty:
                raise ValueError("nessun dato giornaliero")
            try:
                intraday = t.history(period="5d", interval="30m", auto_adjust=True)
            except Exception:
                intraday = None
            return t, daily, intraday
        except Exception as e:
            log(f"  {simbolo}: tentativo {i + 1} fallito ({e})")
            time.sleep(3 * (i + 1))
    return t, None, None


def valuta(t):
    try:
        return t.fast_info["currency"] or ""
    except Exception:
        return ""


UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"}


_FONTI_KO = {}  # fonte -> fallimenti consecutivi in questo run


def _leggi_rss(nome_fonte, url, max_n, fonte_default=""):
    """Legge un feed RSS; dopo 2 errori consecutivi la fonte viene saltata per il resto del run."""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    from urllib.parse import urlparse, parse_qs
    if _FONTI_KO.get(nome_fonte, 0) >= 2:
        return []
    try:
        r = requests.get(url, headers=UA, timeout=8)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        _FONTI_KO[nome_fonte] = 0
    except Exception as e:
        _FONTI_KO[nome_fonte] = _FONTI_KO.get(nome_fonte, 0) + 1
        if _FONTI_KO[nome_fonte] == 2:
            log(f"  fonte notizie {nome_fonte} non raggiungibile ({e}): la salto per questo run")
        return []
    out = []
    for it in root.iter("item"):
        titolo = (it.findtext("title") or "").strip()
        if not titolo:
            continue
        fonte = fonte_default
        for el in it:
            if el.tag.split("}")[-1].lower() == "source" and (el.text or "").strip():
                fonte = el.text.strip()
        if fonte and titolo.endswith(" - " + fonte):
            titolo = titolo[: -len(fonte) - 3]
        link = it.findtext("link") or ""
        if "bing.com" in link and "url=" in link:  # link di Bing: estrae l'URL vero
            link = parse_qs(urlparse(link).query).get("url", [link])[0]
        try:
            data = parsedate_to_datetime(it.findtext("pubDate")).isoformat()
        except Exception:
            data = ""
        out.append({"titolo": titolo, "url": link, "fonte": fonte, "data": data})
        if len(out) >= max_n:
            break
    return out


def notizie_web(query, simbolo=None, max_n=4):
    """Notizie recenti da fonti RSS alternative, in ordine di affidabilità dai server GitHub."""
    from urllib.parse import urlencode, quote
    tentativi = []
    if simbolo:
        tentativi.append(("Yahoo RSS", "https://feeds.finance.yahoo.com/rss/2.0/headline?"
                          + urlencode({"s": simbolo, "region": "US", "lang": "en-US"}), "Yahoo Finance"))
    if query:
        tentativi.append(("Bing News", "https://www.bing.com/news/search?" + urlencode({"q": query, "format": "rss"}), ""))
        tentativi.append(("Google News", "https://news.google.com/rss/search?"
                          + urlencode({"q": f"{query} when:7d", "hl": "en-US", "gl": "US", "ceid": "US:en"}), ""))
    for nome, url, predef in tentativi:
        out = _leggi_rss(nome, url, max_n, predef)
        if out:
            return out
    return []


def notizie(t, max_n, query=None):
    out = []
    try:
        items = t.news or []
    except Exception:
        items = []
    for it in items:
        if len(out) >= max_n:
            break
        c = it.get("content") or it
        titolo = c.get("title")
        if not titolo:
            continue
        cu = c.get("canonicalUrl")
        url = (cu.get("url") if isinstance(cu, dict) else None) or c.get("link") or ""
        prov = c.get("provider")
        fonte = (prov.get("displayName") if isinstance(prov, dict) else None) or c.get("publisher") or ""
        data = c.get("pubDate") or ""
        if not data and it.get("providerPublishTime"):
            data = datetime.fromtimestamp(it["providerPublishTime"], timezone.utc).isoformat()
        out.append({"titolo": titolo, "url": url, "fonte": fonte, "data": data})
    if not out:
        out = notizie_web(query, getattr(t, "ticker", None), max_n)
    return out


def query_notizie(voce):
    """Testo di ricerca per le news in base al tipo di strumento."""
    nome = voce.get("nome") or voce["simbolo"]
    m = voce.get("mercato", "")
    if m == "Crypto":
        return f"{nome} crypto"
    if m in ("Valute", "Materie prime", "Indice", "ETF"):
        return nome
    return f'"{nome}" stock'


def rsi(close, n=14):
    delta = close.diff()
    su = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    giu = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + su / giu)


def calcola(daily, intraday, crypto=False):
    close = daily["Close"].dropna()
    if len(close) < 2:
        raise ValueError("storico insufficiente")
    p = float(close.iloc[-1])
    prec = float(close.iloc[-2])

    def var(n):
        return num((p / float(close.iloc[-1 - n]) - 1) * 100, 2) if len(close) > n else None

    anno = close.index[-1].year
    prima = close[close.index.year < anno]
    ytd = num((p / float(prima.iloc[-1]) - 1) * 100, 2) if len(prima) else None

    sma20 = close.rolling(20).mean()
    sma50 = close.rolling(50).mean()
    sma200 = close.rolling(200).mean()
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    segnale = macd.ewm(span=9, adjust=False).mean()
    hist = macd - segnale
    r = rsi(close)

    rend = close.pct_change().dropna()
    giorni_anno = 365 if crypto else 252
    sd20 = float(rend.tail(20).std()) if len(rend) >= 20 else None
    vol20 = sd20 * math.sqrt(giorni_anno) * 100 if sd20 else None
    var1 = (p / prec - 1) * 100
    z = var1 / (sd20 * 100) if sd20 else None

    ult = close.tail(252)
    max52, min52 = float(ult.max()), float(ult.min())

    vol_rel = None
    if "Volume" in daily:
        v = daily["Volume"].fillna(0)
        if len(v) > 21:
            media = float(v.iloc[-21:-1].mean())
            if media > 0:
                vol_rel = float(v.iloc[-1]) / media

    s20, s50, s200 = (num(s.iloc[-1]) for s in (sma20, sma50, sma200))
    rv = num(r.iloc[-1], 1)

    # ---- segnali (regole fisse, niente AI)
    sg = []

    def add(tipo, testo):
        sg.append({"tipo": tipo, "testo": testo})

    if rv is not None:
        if rv >= 70:
            add("negativo", f"RSI {rv:.0f}: zona di ipercomprato")
        elif rv <= 30:
            add("positivo", f"RSI {rv:.0f}: zona di ipervenduto")
    h = hist.tail(3)
    if len(h) == 3:
        if h.iloc[-1] > 0 and (h.iloc[:-1] <= 0).any():
            add("positivo", "MACD incrocia al rialzo la linea di segnale")
        elif h.iloc[-1] < 0 and (h.iloc[:-1] >= 0).any():
            add("negativo", "MACD incrocia al ribasso la linea di segnale")
    if len(close) > 54 and not math.isnan(sma50.iloc[-4]):
        prima_sopra = close.iloc[-4] > sma50.iloc[-4]
        ora_sopra = p > sma50.iloc[-1]
        if ora_sopra and not prima_sopra:
            add("positivo", "Prezzo tornato sopra la media a 50 giorni")
        elif prima_sopra and not ora_sopra:
            add("negativo", "Prezzo sceso sotto la media a 50 giorni")
    if len(close) > 205:
        d_ora = sma50.iloc[-1] - sma200.iloc[-1]
        d_prima = sma50.iloc[-6] - sma200.iloc[-6]
        if d_ora > 0 >= d_prima:
            add("positivo", "Golden cross: SMA50 sopra SMA200")
        elif d_ora < 0 <= d_prima:
            add("negativo", "Death cross: SMA50 sotto SMA200")
    if s50 and s200:
        if p > s50 > s200:
            add("positivo", "Trend di fondo rialzista (prezzo > SMA50 > SMA200)")
        elif p < s50 < s200:
            add("negativo", "Trend di fondo ribassista (prezzo < SMA50 < SMA200)")
    if z is not None and abs(z) >= 2:
        add("positivo" if z > 0 else "negativo",
            f"Movimento di oggi anomalo: {abs(z):.1f} volte la volatilità tipica")
    if vol_rel is not None and vol_rel >= 2:
        add("neutro", f"Volumi {vol_rel:.1f}× la media a 20 giorni")
    if p >= max52 * 0.98:
        add("positivo", "Vicino ai massimi a 52 settimane")
    elif p <= min52 * 1.02:
        add("negativo", "Vicino ai minimi a 52 settimane")

    # ---- serie per i grafici
    serie = [[d.strftime("%Y-%m-%d"), num(c, 4)] for d, c in close.items()]
    infra = []
    if intraday is not None and not intraday.empty:
        ic = intraday["Close"].dropna()
        infra = [[int(ts.timestamp()), num(c, 4)] for ts, c in ic.items()]

    return {
        "prezzo": num(p),
        "chiusura_prec": num(prec),
        "var": {"1g": num(var1, 2), "5g": var(5), "1m": var(21), "3m": var(63), "ytd": ytd},
        "indicatori": {
            "rsi14": rv,
            "sma20": s20, "sma50": s50, "sma200": s200,
            "macd": num(macd.iloc[-1]), "macd_segnale": num(segnale.iloc[-1]), "macd_hist": num(hist.iloc[-1]),
            "volatilita20": num(vol20, 1),
            "max52": num(max52), "min52": num(min52),
            "dist_max52": num((p / max52 - 1) * 100, 2),
            "volume_rel": num(vol_rel, 2),
            "z_giorno": num(z, 2),
        },
        "segnali": sg,
        "serie": serie,
        "intraday": infra,
        "ultimo_dato": close.index[-1].strftime("%Y-%m-%d"),
    }


# -------------------------------------------------------------------- AI

SISTEMA = """Sei un analista finanziario che scrive per un investitore privato italiano.
Ricevi dati di mercato già calcolati (prezzi, variazioni %, indicatori tecnici, segnali) e i titoli delle notizie recenti.

Regole:
- Usa SOLO i numeri presenti nei dati. Non inventare prezzi, livelli, percentuali o eventi.
- Distingui i fatti (dati) dalle interpretazioni.
- Non dare ordini di acquisto/vendita né target di prezzo: descrivi il quadro, i rischi e cosa monitorare.
- Se le notizie sono poche o generiche, non trarne conclusioni forti.
- Scrivi in italiano, in modo concreto e sintetico, con i numeri in formato italiano (virgola decimale: 7,8%).
- Rispondi compilando lo strumento salva_analisi."""

SCHEMA = """{
  "sintesi": "3-5 frasi sul quadro generale di tutti i mercati monitorati",
  "umore": "positivo | neutro | negativo",
  "cambiamenti": "1-2 frasi su cosa è cambiato rispetto all'analisi precedente (stringa vuota se non c'è un'analisi precedente)",
  "titoli": [
    {
      "simbolo": "uguale al simbolo nei dati",
      "giudizio": "rialzista | neutrale | ribassista",
      "commento": "2-3 frasi che collegano indicatori, segnali e notizie",
      "da_monitorare": "una frase: livello o evento da tenere d'occhio, citando solo numeri presenti nei dati"
    }
  ],
  "in_evidenza": ["massimo 3 simboli che meritano attenzione adesso"]
}"""


STRUMENTO = {
    "name": "salva_analisi",
    "description": "Salva l'analisi di mercato strutturata.",
    "input_schema": {
        "type": "object",
        "properties": {
            "sintesi": {"type": "string", "description": "3-5 frasi sul quadro generale"},
            "umore": {"type": "string", "enum": ["positivo", "neutro", "negativo"]},
            "cambiamenti": {"type": "string", "description": "Cosa è cambiato rispetto all'analisi precedente, stringa vuota se non c'è"},
            "titoli": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "simbolo": {"type": "string"},
                        "giudizio": {"type": "string", "enum": ["rialzista", "neutrale", "ribassista"]},
                        "commento": {"type": "string"},
                        "da_monitorare": {"type": "string"},
                    },
                    "required": ["simbolo", "giudizio", "commento", "da_monitorare"],
                },
            },
            "in_evidenza": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        },
        "required": ["sintesi", "umore", "cambiamenti", "titoli", "in_evidenza"],
    },
}


def risposta_strumento(resp):
    """Estrae l'input dello strumento; in mancanza prova a leggere JSON dal testo."""
    for b in resp.content:
        if b.type == "tool_use":
            return dict(b.input)
    if getattr(resp, "stop_reason", "") == "max_tokens":
        raise ValueError("risposta troncata: aumenta max_token_risposta in config.json")
    return estrai_json("".join(b.text for b in resp.content if b.type == "text"))


def compatta(t):
    """Dati ridotti da mandare al modello (niente serie storiche lunghe)."""
    return {
        "simbolo": t["simbolo"], "nome": t["nome"], "mercato": t["mercato"], "valuta": t.get("valuta", ""),
        "prezzo": t["prezzo"], "chiusura_prec": t["chiusura_prec"], "ultimo_dato": t.get("ultimo_dato"),
        "variazioni_pct": t["var"], "indicatori": t["indicatori"],
        "segnali": [s["testo"] for s in t["segnali"]],
        "ultimi_10_close": [c for _, c in t["serie"][-10:]],
        "notizie": [f'{n["data"][:10]} {n["fonte"]}: {n["titolo"]}' for n in t.get("notizie", [])],
        "dati_vecchi": t.get("dati_vecchi", False),
    }


def estrai_json(testo):
    testo = re.sub(r"^```(?:json)?\s*|\s*```$", "", testo.strip())
    i, j = testo.find("{"), testo.rfind("}")
    if i < 0 or j < 0:
        raise ValueError("nessun JSON nella risposta")
    return json.loads(testo[i:j + 1])


def analisi_claude(cfg, titoli, precedente):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        log("ANTHROPIC_API_KEY mancante: salto l'analisi AI")
        return None
    if os.environ.get("SENZA_AI", "").lower() == "true":
        log("Modalità prova senza AI: nessuna chiamata a Claude")
        return None
    if not costi.disponibile(cfg, 0.01):
        log("Budget mensile API esaurito: salto l'analisi AI")
        return None
    import anthropic

    ora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    prec = ""
    if precedente:
        prec = (f"\nAnalisi precedente ({precedente.get('generata', '?')}):\n"
                f"{precedente.get('sintesi', '')}\n"
                f"Giudizi: {json.dumps({x['simbolo']: x.get('giudizio') for x in precedente.get('titoli', [])}, ensure_ascii=False)}\n")
    prompt = (f"Data e ora: {ora}\n{prec}\n"
              f"Dati attuali:\n{json.dumps([compatta(t) for t in titoli], ensure_ascii=False)}\n\n"
              f"Contenuto atteso:\n{SCHEMA}")

    client = anthropic.Anthropic(api_key=key)
    for i in range(2):
        try:
            resp = client.messages.create(
                model=cfg["modello"],
                max_tokens=cfg.get("max_token_risposta", 4000),
                system=SISTEMA,
                messages=[{"role": "user", "content": prompt}],
                tools=[STRUMENTO],
                tool_choice={"type": "tool", "name": STRUMENTO["name"]},
            )
            costi.registra(cfg, cfg["modello"], resp.usage, "monitor")
            ai = risposta_strumento(resp)
            ai["generata"] = ora_iso()
            ai["modello"] = cfg["modello"]
            ai["token"] = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}
            return ai
        except Exception as e:
            log(f"Analisi AI fallita (tentativo {i + 1}): {e}")
            time.sleep(5)
    return None


def minuti_da(iso):
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds() / 60
    except Exception:
        return 1e9


# ------------------------------------------------------------------- main

def main():
    cfg = carica_json(CONFIG, None)
    if not cfg or not cfg.get("titoli"):
        log("config.json mancante o senza titoli")
        sys.exit(1)

    prev = carica_json(DATA, {})
    prev_titoli = {t["simbolo"]: t for t in prev.get("titoli", [])}
    titoli = []

    for voce in cfg["titoli"]:
        s = voce["simbolo"]
        log(f"Scarico {s}")
        t, daily, intraday = scarica(s)
        if daily is None:
            if s in prev_titoli:
                vecchio = dict(prev_titoli[s])
                vecchio["dati_vecchi"] = True
                titoli.append(vecchio)
                log(f"  {s}: uso i dati precedenti")
            continue
        try:
            dati = calcola(daily, intraday, crypto=voce.get("mercato") == "Crypto")
        except Exception as e:
            log(f"  {s}: errore nel calcolo ({e})")
            continue
        dati.update({
            "simbolo": s,
            "nome": voce.get("nome", s),
            "mercato": voce.get("mercato", ""),
            "valuta": voce.get("valuta") or valuta(t),
            "notizie": notizie(t, cfg.get("notizie_per_titolo", 5), query=query_notizie(voce)),
            "dati_vecchi": False,
        })
        titoli.append(dati)

    if not titoli:
        log("Nessun dato scaricato: esco senza modificare i file")
        sys.exit(0)

    # ---- decide se rifare l'analisi AI
    ai_prec = prev.get("ai")
    forza = os.environ.get("FORZA_AI", "").lower() == "true"
    invariato = all(prev_titoli.get(t["simbolo"], {}).get("prezzo") == t["prezzo"] for t in titoli)
    troppo_presto = ai_prec and minuti_da(ai_prec.get("generata", "")) < cfg.get("ai_ogni_minuti", 60) - 5

    ai = None
    if forza or not ai_prec:
        ai = analisi_claude(cfg, titoli, ai_prec)
    elif cfg.get("salta_ai_se_invariato", True) and invariato:
        log("Prezzi invariati: tengo l'analisi precedente")
    elif troppo_presto:
        log("Analisi AI recente: aggiorno solo i dati")
    else:
        ai = analisi_claude(cfg, titoli, ai_prec)

    nuova_ai = ai is not None
    if not nuova_ai:
        ai = ai_prec

    salva_json(DATA, {"aggiornato": ora_iso(), "titoli": titoli, "ai": ai})

    if nuova_ai:
        storico = carica_json(STORICO, [])
        storico.append({
            "data": ai["generata"],
            "umore": ai.get("umore"),
            "sintesi": ai.get("sintesi"),
            "cambiamenti": ai.get("cambiamenti"),
            "in_evidenza": ai.get("in_evidenza", []),
            "giudizi": {x["simbolo"]: x.get("giudizio") for x in ai.get("titoli", [])},
            "prezzi": {t["simbolo"]: t["prezzo"] for t in titoli},
        })
        salva_json(STORICO, storico[-cfg.get("storico_max", 300):])
        log(f"Analisi AI salvata ({ai['token']['input']} token in, {ai['token']['output']} out)")
    elif not STORICO.exists():
        salva_json(STORICO, [])

    log(f"Fatto: {len(titoli)} titoli")


if __name__ == "__main__":
    main()
