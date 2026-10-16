"""
Dati extra per i candidati migliori (chiamate lente: solo per poche decine di titoli).
"""
import sys
from datetime import date, datetime
from pathlib import Path

import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analisi import num  # noqa: E402

AZIONI = ("USA", "Italia", "Europa")

CAMPI = {
    "settore": "sector", "industria": "industry", "capitalizzazione": "marketCap",
    "pe": "trailingPE", "pe_forward": "forwardPE", "crescita_ricavi": "revenueGrowth",
    "crescita_utili": "earningsGrowth", "margine_netto": "profitMargins", "debito_su_equity": "debtToEquity",
    "short_float": "shortPercentOfFloat", "beta": "beta", "consenso_analisti": "recommendationKey",
    "target_medio_analisti": "targetMeanPrice", "n_analisti": "numberOfAnalystOpinions",
}


def prossima_trimestrale(simbolo):
    """Data della prossima trimestrale (date) o None."""
    try:
        cal = yf.Ticker(simbolo).calendar
    except Exception:
        return None
    date_ = cal.get("Earnings Date") if isinstance(cal, dict) else None
    if not date_:
        return None
    oggi = date.today()
    future = []
    for d in date_ if isinstance(date_, (list, tuple)) else [date_]:
        if isinstance(d, datetime):
            d = d.date()
        if isinstance(d, date) and d >= oggi:
            future.append(d)
    return min(future) if future else None


def fondamentali(simbolo):
    try:
        info = yf.Ticker(simbolo).info or {}
    except Exception:
        return {}
    out = {}
    for k, src in CAMPI.items():
        v = info.get(src)
        if v is None:
            continue
        out[k] = v if isinstance(v, str) else num(v, 3)
    return out
