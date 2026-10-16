"""
Universo degli strumenti da scansionare.
- Azioni USA: lista completa dallo screener del Nasdaq, filtrata per capitalizzazione e volumi
- Italia, Europa, ETF, indici, crypto, valute, materie prime: liste fisse in liste.json
Ricostruito al massimo una volta a settimana e salvato in docs/universo.json.
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analisi import log, carica_json, salva_json, UA  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LISTE = Path(__file__).resolve().parent / "liste.json"
UNIVERSO = ROOT / "docs" / "universo.json"

# settori dello screener Nasdaq -> ETF settoriale per la forza relativa
SETTORI_ETF = {
    "Technology": "XLK", "Finance": "XLF", "Health Care": "XLV", "Energy": "XLE",
    "Industrials": "XLI", "Consumer Discretionary": "XLY", "Consumer Staples": "XLP",
    "Utilities": "XLU", "Basic Materials": "XLB", "Real Estate": "XLRE", "Telecommunications": "XLC",
}
ESCLUDI = re.compile(r"\b(warrants?|preferred|rights?|units?|notes due|debentures|depositary shares? each representing)\b", re.I)


def _numero(x):
    try:
        return float(str(x).replace(",", "").replace("$", "").strip() or 0)
    except ValueError:
        return 0.0


def _pulisci_nome(n):
    n = re.sub(r"\s+(Class [A-Z]\s+)?(Common|Ordinary|Capital)\s+(Stock|Shares).*$", "", n, flags=re.I)
    n = re.sub(r"\s+American Depositary Shares?.*$", "", n, flags=re.I)
    return n.strip(" ,")


def azioni_usa(cap_min, vol_min, max_n):
    r = requests.get("https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true",
                     headers={**UA, "Accept": "application/json"}, timeout=40)
    r.raise_for_status()
    righe = (r.json().get("data") or {}).get("rows") or []
    out = []
    for x in righe:
        sim = (x.get("symbol") or "").strip()
        nome = (x.get("name") or "").strip()
        if not sim or "^" in sim or " " in sim or ESCLUDI.search(nome):
            continue
        cap, vol = _numero(x.get("marketCap")), _numero(x.get("volume"))
        if cap < cap_min or vol < vol_min:
            continue
        out.append({
            "simbolo": sim.replace("/", "-").replace(".", "-"),
            "nome": _pulisci_nome(nome) or sim,
            "mercato": "USA",
            "settore": x.get("sector") or "",
            "cap": cap,
        })
    out.sort(key=lambda v: v["cap"], reverse=True)
    return out[:max_n]


def costruisci(cfg_b, forza=False):
    vecchio = carica_json(UNIVERSO, {})
    try:
        eta = (datetime.now(timezone.utc) - datetime.fromisoformat(vecchio["generato"])).days
    except Exception:
        eta = 999
    if not forza and eta < cfg_b.get("universo_giorni", 7) and vecchio.get("voci"):
        log(f"Universo di {eta} giorni fa: {len(vecchio['voci'])} strumenti")
        return vecchio["voci"]

    liste = json.loads(LISTE.read_text(encoding="utf-8"))
    try:
        usa = azioni_usa(cfg_b.get("usa_cap_min", 2e9), cfg_b.get("usa_volume_min", 200_000), cfg_b.get("usa_max", 2000))
        log(f"Azioni USA dallo screener Nasdaq: {len(usa)}")
    except Exception as e:
        usa = [v for v in vecchio.get("voci", []) if v.get("mercato") == "USA"]
        log(f"Screener Nasdaq non disponibile ({e}): uso la lista precedente ({len(usa)})")
        if not usa:
            usa = [v for v in cfg_b.get("universo", []) if v.get("mercato") == "USA"]

    voci, visti = [], set()
    for gruppo in [usa] + [liste[k] for k in ("italia", "europa", "etf", "indici", "crypto", "valute", "materie_prime")]:
        for v in gruppo:
            if v["simbolo"] not in visti:
                visti.add(v["simbolo"])
                voci.append(v)
    conteggi = {}
    for v in voci:
        conteggi[v["mercato"]] = conteggi.get(v["mercato"], 0) + 1
    salva_json(UNIVERSO, {"generato": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          "conteggi": conteggi, "voci": voci})
    log(f"Universo ricostruito: {len(voci)} strumenti {conteggi}")
    return voci
