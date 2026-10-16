"""
Dati extra per i candidati migliori (chiamate lente: solo per poche decine di titoli).

Yahoo blocca spesso date trimestrali e fondamentali dai server GitHub (401 Invalid Crumb),
quindi per le azioni USA la fonte principale è il Nasdaq; Yahoo resta come riserva.
Una trimestrale che non si riesce a verificare è "sconosciuta", mai "assente".
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analisi import num, log, UA  # noqa: E402

AZIONI = ("USA", "Italia", "Europa")
NASDAQ = {**UA, "Accept": "application/json", "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}

CAMPI = {
    "settore": "sector", "industria": "industry", "capitalizzazione": "marketCap",
    "pe": "trailingPE", "pe_forward": "forwardPE", "crescita_ricavi": "revenueGrowth",
    "crescita_utili": "earningsGrowth", "margine_netto": "profitMargins", "debito_su_equity": "debtToEquity",
    "short_float": "shortPercentOfFloat", "beta": "beta", "consenso_analisti": "recommendationKey",
    "target_medio_analisti": "targetMeanPrice", "n_analisti": "numberOfAnalystOpinions",
}

_calendario = None   # {simbolo_normalizzato: date} per il run corrente
_calendario_ok = False


def _norm(s):
    return s.upper().replace("/", "-").replace(".", "-")


def calendario_nasdaq(giorni):
    """Trimestrali USA dei prossimi `giorni` giorni dal calendario Nasdaq (una richiesta per giorno feriale)."""
    global _calendario, _calendario_ok
    if _calendario is not None:
        return _calendario
    _calendario, riusciti, feriali = {}, 0, 0
    oggi = date.today()
    for k in range(giorni + 1):
        g = oggi + timedelta(days=k)
        if g.weekday() >= 5:
            continue
        feriali += 1
        try:
            r = requests.get(f"https://api.nasdaq.com/api/calendar/earnings?date={g.isoformat()}", headers=NASDAQ, timeout=20)
            r.raise_for_status()
            righe = ((r.json() or {}).get("data") or {}).get("rows") or []
            riusciti += 1
            for x in righe:
                s = _norm(x.get("symbol") or "")
                if s and s not in _calendario:
                    _calendario[s] = g
        except Exception as e:
            log(f"  calendario Nasdaq {g}: {e}")
    _calendario_ok = feriali > 0 and riusciti == feriali
    log(f"Calendario trimestrali Nasdaq: {len(_calendario)} società in {riusciti}/{feriali} giorni"
        + ("" if _calendario_ok else " (INCOMPLETO: le date mancanti risultano sconosciute)"))
    return _calendario


def _trimestrale_yahoo(simbolo):
    """(data, ok): ok=False se Yahoo non ha risposto in modo utilizzabile."""
    try:
        cal = yf.Ticker(simbolo).calendar
    except Exception:
        return None, False
    date_ = cal.get("Earnings Date") if isinstance(cal, dict) else None
    if not date_:
        return None, False
    oggi = date.today()
    future = []
    for d in date_ if isinstance(date_, (list, tuple)) else [date_]:
        if isinstance(d, datetime):
            d = d.date()
        if isinstance(d, date) and d >= oggi:
            future.append(d)
    return (min(future) if future else None), True


def prossima_trimestrale(simbolo, mercato, giorni):
    """
    Restituisce (data, stato):
      (d, "data")            trimestrale prevista il giorno d
      (None, "nessuna")      verificato: nessuna trimestrale nei prossimi `giorni` giorni
      (None, "sconosciuta")  non è stato possibile verificarlo
    """
    if mercato == "USA":
        d = calendario_nasdaq(giorni).get(_norm(simbolo))
        if d:
            return d, "data"
        if _calendario_ok:
            return None, "nessuna"
    d, ok = _trimestrale_yahoo(simbolo)
    if d:
        return d, "data"
    return None, ("nessuna" if ok else "sconosciuta")


def _valore(x):
    if not isinstance(x, dict):
        return None
    grezzo = str(x.get("value") or "").strip()
    v = grezzo.replace("$", "").replace(",", "").replace("%", "").strip()
    if not v or v.upper() in ("N/A", "NA", "--"):
        return None
    try:
        return num(float(v), 3)
    except ValueError:
        return grezzo


def fondamentali_nasdaq(simbolo):
    r = requests.get(f"https://api.nasdaq.com/api/quote/{simbolo.replace('-', '.')}/summary?assetclass=stocks",
                     headers=NASDAQ, timeout=20)
    r.raise_for_status()
    s = (((r.json() or {}).get("data") or {}).get("summaryData")) or {}
    mappa = {"settore": "Sector", "industria": "Industry", "pe": "PERatio", "pe_forward": "ForwardPE1Yr",
             "eps": "EarningsPerShare", "target_1_anno": "OneYrTarget", "beta": "Beta",
             "rendimento_dividendo": "Yield", "capitalizzazione": "MarketCap"}
    out = {}
    for k, src in mappa.items():
        v = _valore(s.get(src))
        if v is not None:
            out[k] = v
    return out


def fondamentali(simbolo, mercato="USA"):
    out = {}
    try:
        info = yf.Ticker(simbolo).info or {}
        for k, src in CAMPI.items():
            v = info.get(src)
            if v is not None:
                out[k] = v if isinstance(v, str) else num(v, 3)
    except Exception:
        pass
    if len(out) < 3 and mercato == "USA":
        try:
            nas = fondamentali_nasdaq(simbolo)
            if nas:
                out = {**nas, **out, "fonte": "Nasdaq"}
        except Exception as e:
            log(f"  fondamentali Nasdaq {simbolo}: {e}")
    return out
