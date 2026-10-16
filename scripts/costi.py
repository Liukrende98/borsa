"""
Contabilità della spesa API Claude, mese per mese, in docs/costi.json.
Ogni chiamata registra il suo costo; prima di una chiamata costosa si controlla il budget.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COSTI = ROOT / "docs" / "costi.json"

# USD per milione di token [input, output] - sovrascrivibili da config.json -> prezzi_usd_mtok
PREZZI = {
    "claude-haiku-5-5": [0.10, 0.50],
    "claude-sonnet-5-5": [2.00, 10.00],
    "claude-opus-5-5": [4.00, 20.00],
}


def _mese():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def carica():
    try:
        d = json.loads(COSTI.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    if d.get("mese") != _mese():
        storico = d.get("storico", [])
        if d.get("mese"):
            storico = (storico + [{"mese": d["mese"], "totale_usd": d.get("totale_usd", 0)}])[-12:]
        d = {"mese": _mese(), "totale_usd": 0.0, "voci": {}, "chiamate": 0, "storico": storico}
    return d


def prezzo(cfg, modello):
    return (cfg.get("prezzi_usd_mtok") or {}).get(modello) or PREZZI.get(modello) or [5.0, 25.0]


def registra(cfg, modello, usage, voce):
    p = prezzo(cfg, modello)
    tin = getattr(usage, "input_tokens", 0) or 0
    tout = getattr(usage, "output_tokens", 0) or 0
    # Haiku 5.5 costa 5 volte tanto oltre i 100k token di prompt
    molt = 5 if "haiku-5" in modello and tin > 100_000 else 1
    costo = (tin * p[0] + tout * p[1]) / 1e6 * molt
    d = carica()
    d["totale_usd"] = round(d["totale_usd"] + costo, 4)
    d["voci"][voce] = round(d["voci"].get(voce, 0) + costo, 4)
    d["chiamate"] += 1
    d["budget_usd"] = cfg.get("budget_mensile_usd", 20)
    d["aggiornato"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    COSTI.parent.mkdir(parents=True, exist_ok=True)
    COSTI.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    return costo


def disponibile(cfg, stima_usd=0.0):
    """True se la spesa del mese più la stima resta entro il budget."""
    return carica()["totale_usd"] + stima_usd <= cfg.get("budget_mensile_usd", 20)
