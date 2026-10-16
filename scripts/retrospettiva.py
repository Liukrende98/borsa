"""
Retrospettiva settimanale: Claude rilegge le idee chiuse, trova cosa funziona e cosa no,
scrive lezioni (usate dagli agenti nei giorni successivi) e proposte di modifica alle regole
(che restano proposte: le applichi tu).
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analisi import log, carica_json, salva_json, ora_iso  # noqa: E402
import ai  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LEZIONI = ROOT / "docs" / "lezioni.json"
CHIUSE = ("target", "stop", "scaduta")

SISTEMA = """Sei il responsabile del rischio di un piccolo sistema di trading di breve termine.
Ricevi le statistiche delle idee chiuse, raggruppate per setup, settore, mercato, regime, direzione, fiducia e fase di scarto, più un campione di idee.
"Tenute" = idee selezionate; "scartate dall'analista" e "scartate dal critico" = idee rifiutate ma simulate lo stesso.
Obiettivo: capire dove il sistema guadagna e dove perde, e se i filtri AI aggiungono valore.

Regole:
- Ragiona sui numeri forniti, non inventarne. Con meno di 10 casi un gruppo è aneddotico: dillo.
- Le lezioni devono essere istruzioni operative brevi che un analista può applicare domani (es. "evitare breakout long quando il VIX è sopra 25: 2 vinte su 9").
- Le proposte sono modifiche concrete a regole o parametri (soglie, setup da disattivare, filtri).
- Italiano, numeri con la virgola decimale. Rispondi compilando lo strumento salva_retrospettiva."""

STRUMENTO = {
    "name": "salva_retrospettiva",
    "description": "Salva le conclusioni della retrospettiva.",
    "input_schema": {
        "type": "object",
        "properties": {
            "sintesi": {"type": "string", "description": "3-5 frasi: come sta andando il sistema"},
            "lezioni": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
            "proposte": {
                "type": "array", "maxItems": 5,
                "items": {"type": "object",
                          "properties": {"modifica": {"type": "string"}, "motivo": {"type": "string"}},
                          "required": ["modifica", "motivo"]},
            },
        },
        "required": ["sintesi", "lezioni", "proposte"],
    },
}


def _gruppo(lista, chiave):
    g = {}
    for i in lista:
        k = str(i.get(chiave) or "n/d")
        g.setdefault(k, []).append(i)
    out = {}
    for k, v in g.items():
        r = [x["r"] for x in v if x.get("r") is not None]
        out[k] = {"n": len(v), "vinte": sum(1 for x in v if (x.get("rendimento_pct") or 0) > 0),
                  "r_medio": round(sum(r) / len(r), 2) if r else None}
    return out


def esegui(cfg, idee, forza=False):
    cfg_b = cfg.get("breve", {})
    vecchie = carica_json(LEZIONI, {})
    try:
        eta = (datetime.now(timezone.utc) - datetime.fromisoformat(vecchie["generato"])).days
    except Exception:
        eta = 999
    if not forza and eta < 6:
        return vecchie

    limite = (datetime.now(timezone.utc) - timedelta(days=cfg_b.get("retrospettiva_giorni", 90))).strftime("%Y-%m-%d")
    chiuse = [i for i in idee if i.get("stato") in CHIUSE and i.get("data_segnale", "") >= limite]
    if len(chiuse) < cfg_b.get("retrospettiva_min_chiuse", 10):
        log(f"Retrospettiva rimandata: solo {len(chiuse)} idee chiuse")
        return vecchie

    for i in chiuse:
        i["_gruppo"] = "tenute" if i.get("ai_tenuta") is not False else (
            "scartate dal critico" if i.get("fase_scarto") == 2 else "scartate dall'analista")
        i["_fid"] = i.get("fiducia")
    stat = {k: _gruppo(chiuse, c) for k, c in [
        ("per_gruppo", "_gruppo"), ("per_setup", "setup"), ("per_mercato", "mercato"), ("per_settore", "settore"),
        ("per_regime", "regime"), ("per_direzione", "direzione"), ("per_fiducia", "_fid")]}
    campione = [{k: i.get(k) for k in ("data_segnale", "nome", "setup", "direzione", "mercato", "settore", "regime",
                                       "fiducia", "_gruppo", "stato", "r", "rendimento_pct", "motivazione", "critica")}
                for i in chiuse[-60:]]
    for i in chiuse:
        i.pop("_gruppo", None)
        i.pop("_fid", None)

    prompt = (f"Idee chiuse analizzate: {len(chiuse)} (dal {limite})\n\n"
              f"Statistiche:\n{json.dumps(stat, ensure_ascii=False)}\n\n"
              f"Campione (ultime 60):\n{json.dumps(campione, ensure_ascii=False)}\n\n"
              f"Lezioni della settimana scorsa:\n{json.dumps(vecchie.get('lezioni', []), ensure_ascii=False)}")
    dati, _ = ai.chiama(cfg, cfg_b.get("modello_retrospettiva", "claude-opus-5-5"), SISTEMA, prompt, STRUMENTO,
                        "retrospettiva", max_tokens=16000, effort=cfg_b.get("ragionamento", "high"), stima_usd=0.5)
    if not dati:
        return vecchie
    dati.update({"generato": ora_iso(), "basata_su": len(chiuse)})
    salva_json(LEZIONI, dati)
    log(f"Retrospettiva: {len(dati.get('lezioni', []))} lezioni, {len(dati.get('proposte', []))} proposte")
    return dati
