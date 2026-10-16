#!/usr/bin/env python3
"""
Agente breve termine (1-10 sedute).

Ogni sera dopo la chiusura USA:
1. VALUTA le idee aperte nel registro con le candele nuove (target / stop / scadenza)
2. SCANSIONA l'universo con setup a regole fisse (breakout, pullback, rimbalzi)
3. Claude FILTRA i candidati (tiene/scarta, fiducia, motivo, rischio) senza toccare i livelli
4. Registra TUTTE le idee, anche quelle scartate, così si misura se il filtro AI aggiunge valore

Simulazione: ingresso all'apertura della seduta successiva, senza leva, costo fisso per trade.
"""
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analisi import num, carica_json, salva_json, ora_iso, log, rsi, estrai_json, notizie  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.json"
OUT_DIR = ROOT / "docs"
IDEE = OUT_DIR / "idee.json"
BREVE = OUT_DIR / "breve.json"

CHIUSE = ("target", "stop", "scaduta")


def it(x):
    """Prezzo formattato all'italiana per i testi (1.234,56)."""
    d = 5 if abs(x) < 10 else 2
    return f"{x:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def pr(x):
    """Arrotonda un prezzo in base alla sua grandezza (valute con più decimali)."""
    if x is None:
        return None
    return num(x, 5 if abs(x) < 10 else 4 if abs(x) < 1000 else 2)


# ------------------------------------------------------------------ dati

def scarica_giornaliero(simbolo, tentativi=3):
    for i in range(tentativi):
        try:
            df = yf.Ticker(simbolo).history(period="1y", interval="1d", auto_adjust=True)
            df = df.dropna(subset=["Open", "High", "Low", "Close"])
            if df.empty:
                raise ValueError("nessun dato")
            return df
        except Exception as e:
            log(f"  {simbolo}: tentativo {i + 1} fallito ({e})")
            time.sleep(2 * (i + 1))
    return None


def atr(df, n=14):
    pc = df["Close"].shift(1)
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def volume_rel(df):
    if "Volume" not in df:
        return None
    v = df["Volume"].fillna(0)
    if len(v) < 22:
        return None
    media = float(v.iloc[-21:-1].mean())
    return float(v.iloc[-1]) / media if media > 0 else None


# ------------------------------------------------------------- scanner

def scansiona(voce, df, cfg_b):
    """Restituisce i candidati (regole fisse) per un simbolo."""
    if df is None or len(df) < 210:
        return []
    c, h, l, o = df["Close"], df["High"], df["Low"], df["Open"]
    p = float(c.iloc[-1])
    a = float(atr(df).iloc[-1])
    if not a or math.isnan(a):
        return []
    s20 = c.rolling(20).mean()
    s50 = c.rolling(50).mean()
    s200 = c.rolling(200).mean()
    r = rsi(c)
    vr = volume_rel(df)
    max20 = float(h.iloc[-21:-1].max())
    min20 = float(l.iloc[-21:-1].min())
    trend_su = s50.iloc[-1] > s200.iloc[-1] and p > s200.iloc[-1]
    trend_giu = s50.iloc[-1] < s200.iloc[-1] and p < s200.iloc[-1]
    vol_ok = vr is None or vr >= 1.5
    vol_txt = f", volumi {vr:.1f}× la media" if vr else ""
    data = df.index[-1].strftime("%Y-%m-%d")
    rr_min = cfg_b.get("rr_minimo", 1.5)
    out = []

    def idea(setup, direzione, stop, target, motivo):
        d = 1 if direzione == "long" else -1
        rischio = (p - stop) * d
        guadagno = (target - p) * d
        if rischio <= 0 or guadagno <= 0:
            return
        rr = guadagno / rischio
        if rr < rr_min:
            return
        allineato = (d == 1 and trend_su) or (d == -1 and trend_giu)
        punteggio = rr + (min(vr, 4) * 0.5 if vr else 0.5) + (1 if allineato else 0)
        out.append({
            "id": f"{data}-{voce['simbolo']}-{direzione}",
            "agente": "breve",
            "simbolo": voce["simbolo"], "nome": voce.get("nome", voce["simbolo"]), "mercato": voce.get("mercato", ""),
            "direzione": direzione, "setup": setup, "motivo": motivo,
            "data_segnale": data, "prezzo_segnale": pr(p),
            "stop": pr(stop), "target": pr(target),
            "rr": num(rr, 2), "rischio_pct": num(rischio / p * 100, 2),
            "punteggio": num(punteggio, 2),
            "contesto": {
                "rsi14": num(r.iloc[-1], 1), "volume_rel": num(vr, 2), "atr_pct": num(a / p * 100, 2),
                "trend": "rialzista" if trend_su else "ribassista" if trend_giu else "laterale",
                "var_5g": num((p / float(c.iloc[-6]) - 1) * 100, 2),
                "var_1m": num((p / float(c.iloc[-22]) - 1) * 100, 2),
            },
        })

    # 1. Breakout / breakdown del range a 20 sedute
    if p > max20 and vol_ok and p > s50.iloc[-1]:
        idea("Breakout 20 giorni", "long", p - 1.5 * a, p + 3 * a,
             f"Chiusura sopra il massimo delle ultime 20 sedute ({it(max20)}){vol_txt}")
    if p < min20 and vol_ok and p < s50.iloc[-1]:
        idea("Breakdown 20 giorni", "short", p + 1.5 * a, p - 3 * a,
             f"Chiusura sotto il minimo delle ultime 20 sedute ({it(min20)}){vol_txt}")

    # 2. Pullback nel trend
    if trend_su and r.iloc[-4:-1].min() < 40 and p > h.iloc[-2]:
        stop = float(l.iloc[-5:].min()) - 0.3 * a
        idea("Pullback in trend rialzista", "long", stop, p + 2 * (p - stop),
             "Trend rialzista (SMA50 > SMA200), RSI sceso sotto 40 e chiusura sopra il massimo di ieri")
    if trend_giu and r.iloc[-4:-1].max() > 60 and p < l.iloc[-2]:
        stop = float(h.iloc[-5:].max()) + 0.3 * a
        idea("Rimbalzo in trend ribassista", "short", stop, p - 2 * (stop - p),
             "Trend ribassista (SMA50 < SMA200), RSI salito sopra 60 e chiusura sotto il minimo di ieri")

    # 3. Eccessi (mean reversion) contro un trend di fondo favorevole
    if r.iloc[-1] < 30 and p > s200.iloc[-1] and p > o.iloc[-1] and l.iloc[-1] < l.iloc[-2]:
        stop = float(l.iloc[-1]) - 0.5 * a
        target = float(s20.iloc[-1]) if s20.iloc[-1] > p else p + 2 * (p - stop)
        idea("Rimbalzo da ipervenduto", "long", stop, target,
             f"RSI {r.iloc[-1]:.0f} sopra la media a 200 giorni, nuovo minimo con chiusura sopra l'apertura")
    if r.iloc[-1] > 70 and p < s200.iloc[-1] and p < o.iloc[-1] and h.iloc[-1] > h.iloc[-2]:
        stop = float(h.iloc[-1]) + 0.5 * a
        target = float(s20.iloc[-1]) if s20.iloc[-1] < p else p - 2 * (stop - p)
        idea("Storno da ipercomprato", "short", stop, target,
             f"RSI {r.iloc[-1]:.0f} sotto la media a 200 giorni, nuovo massimo con chiusura sotto l'apertura")

    return out


# ----------------------------------------------------------- valutatore

def valuta(idea, df, cfg_b):
    """Aggiorna lo stato di un'idea aperta con le candele successive al segnale."""
    if df is None:
        return
    giorni = df[df.index.strftime("%Y-%m-%d") > idea["data_segnale"]]
    if giorni.empty:
        return
    d = 1 if idea["direzione"] == "long" else -1
    stop, target = idea["stop"], idea["target"]
    giorni_max = cfg_b.get("giorni_max", 10)
    costo = cfg_b.get("costo_pct", 0.1)

    ingresso = float(giorni["Open"].iloc[0])
    # gap che salta già lo stop o il target: l'ingresso non avrebbe senso
    if (ingresso - stop) * d <= 0 or (target - ingresso) * d <= 0:
        idea.update({"stato": "annullata", "data_chiusura": giorni.index[0].strftime("%Y-%m-%d"),
                     "nota": "L'apertura ha già superato stop o target: ingresso saltato"})
        return
    idea["ingresso"] = pr(ingresso)

    def chiudi(stato, prezzo, ts, n):
        rend = ((prezzo / ingresso) - 1) * 100 * d - costo
        rischio = abs(ingresso - stop)
        idea.update({
            "stato": stato, "uscita": pr(prezzo), "data_chiusura": ts.strftime("%Y-%m-%d"),
            "sedute": n, "rendimento_pct": num(rend, 2),
            "r": num((prezzo - ingresso) * d / rischio, 2) if rischio else None,
        })

    for i, (ts, b) in enumerate(giorni.iterrows()):
        o, h, l, c = float(b["Open"]), float(b["High"]), float(b["Low"]), float(b["Close"])
        if i > 0:
            if (o - stop) * d <= 0:
                return chiudi("stop", o, ts, i + 1)
            if (o - target) * d >= 0:
                return chiudi("target", o, ts, i + 1)
        toccato_stop = (l <= stop) if d == 1 else (h >= stop)
        toccato_target = (h >= target) if d == 1 else (l <= target)
        if toccato_stop:  # se nella stessa candela tocca entrambi, ipotesi prudente: stop
            return chiudi("stop", stop, ts, i + 1)
        if toccato_target:
            return chiudi("target", target, ts, i + 1)
        if i + 1 >= giorni_max:
            return chiudi("scaduta", c, ts, i + 1)

    ultimo = float(giorni["Close"].iloc[-1])
    idea.update({
        "sedute": len(giorni), "ultimo_prezzo": pr(ultimo),
        "rendimento_corrente_pct": num(((ultimo / ingresso) - 1) * 100 * d, 2),
    })


def aggrega(lista):
    lista = [i for i in lista if i.get("rendimento_pct") is not None]
    n = len(lista)
    if not n:
        return {"n": 0}
    rend = [i["rendimento_pct"] for i in lista]
    rr = [i["r"] for i in lista if i.get("r") is not None]
    return {
        "n": n,
        "vincenti_pct": num(sum(1 for x in rend if x > 0) / n * 100, 1),
        "rend_medio": num(sum(rend) / n, 2),
        "r_medio": num(sum(rr) / len(rr), 2) if rr else None,
        "r_totale": num(sum(rr), 2) if rr else None,
    }


def statistiche(idee):
    chiuse = [i for i in idee if i.get("stato") in CHIUSE]
    tenute = [i for i in chiuse if i.get("ai_tenuta") is not False]
    scartate = [i for i in chiuse if i.get("ai_tenuta") is False]
    setup = sorted({i["setup"] for i in chiuse})
    return {
        "tenute": aggrega(tenute),
        "scartate": aggrega(scartate),
        "per_setup": {s: aggrega([i for i in tenute if i["setup"] == s]) for s in setup},
        "aperte": sum(1 for i in idee if i.get("stato") == "aperta" and i.get("ai_tenuta") is not False),
    }


# -------------------------------------------------------------------- AI

SISTEMA = """Sei un trader esperto che valuta idee di breve termine (da 1 a 10 sedute) generate da uno scanner tecnico.
Ingresso, stop e target sono calcolati dal codice: non modificarli e non inventare numeri.

Per ogni candidato decidi se tenerlo o scartarlo, assegna una fiducia da 1 a 5 e spiega il motivo e il rischio principale.
Considera:
- coerenza con il quadro degli indici;
- notizie recenti (trimestrali imminenti, eventi societari, notizie che spiegano il movimento);
- correlazione tra le idee: non tenere più idee che sono di fatto la stessa scommessa;
- le statistiche storiche dei setup, se ci sono abbastanza idee chiuse (meno di 20 = poco affidabili).
Sii selettivo: se nessun candidato convince, scartali tutti.
Scrivi in italiano, concreto e sintetico. Rispondi SOLO con un oggetto JSON valido, senza testo prima o dopo."""


def filtro_claude(cfg_b, candidati, contesto, stats):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not candidati:
        if not key:
            log("ANTHROPIC_API_KEY mancante: niente filtro AI")
        return None
    import anthropic

    dati = [{k: c[k] for k in ("id", "simbolo", "nome", "mercato", "direzione", "setup", "motivo",
                               "prezzo_segnale", "stop", "target", "rr", "rischio_pct", "contesto")}
            | {"notizie": [f'{n["data"][:10]} {n["fonte"]}: {n["titolo"]}' for n in c.get("notizie", [])]}
            for c in candidati]
    schema = """{
  "commento": "2-4 frasi sul contesto di breve termine e su come hai scelto",
  "idee": [
    {"id": "uguale all'id del candidato", "tenere": true, "fiducia": 1, "motivazione": "1-2 frasi", "rischio": "1 frase"}
  ]
}"""
    prompt = (f"Indici:\n{json.dumps(contesto, ensure_ascii=False)}\n\n"
              f"Statistiche storiche (idee chiuse):\n{json.dumps(stats, ensure_ascii=False)}\n\n"
              f"Candidati di oggi:\n{json.dumps(dati, ensure_ascii=False)}\n\n"
              f"Tieni al massimo {cfg_b.get('idee_max', 5)} idee. Valuta tutti i candidati.\n"
              f"Schema di risposta:\n{schema}")

    client = anthropic.Anthropic(api_key=key)
    for i in range(2):
        try:
            resp = client.messages.create(
                model=cfg_b.get("modello", "claude-sonnet-5-5"),
                max_tokens=cfg_b.get("max_token_risposta", 4000),
                system=SISTEMA,
                messages=[{"role": "user", "content": prompt}],
            )
            testo = "".join(b.text for b in resp.content if b.type == "text")
            ai = estrai_json(testo)
            ai["token"] = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}
            return ai
        except Exception as e:
            log(f"Filtro AI fallito (tentativo {i + 1}): {e}")
            time.sleep(5)
    return None


# ------------------------------------------------------------------- main

def main():
    cfg = carica_json(CONFIG, {})
    cfg_b = cfg.get("breve") or {}
    universo = cfg_b.get("universo") or []
    if not universo:
        log("config.json: sezione breve.universo mancante")
        sys.exit(1)

    idee = carica_json(IDEE, [])
    dati = {}
    for voce in universo:
        log(f"Scarico {voce['simbolo']}")
        dati[voce["simbolo"]] = scarica_giornaliero(voce["simbolo"])

    # 1. valutazione delle idee aperte
    for idea in idee:
        if idea.get("stato") != "aperta":
            continue
        s = idea["simbolo"]
        if s not in dati:
            dati[s] = scarica_giornaliero(s)
        valuta(idea, dati[s], cfg_b)

    # 2. scansione
    aperti = {i["simbolo"] for i in idee if i.get("stato") == "aperta"}
    gia_visti = {i["id"] for i in idee}
    candidati = []
    for voce in universo:
        if voce["simbolo"] in aperti:
            continue
        try:
            for c in scansiona(voce, dati.get(voce["simbolo"]), cfg_b):
                if c["id"] not in gia_visti:
                    candidati.append(c)
        except Exception as e:
            log(f"  {voce['simbolo']}: errore nello scanner ({e})")
    candidati.sort(key=lambda c: c["punteggio"], reverse=True)
    candidati = candidati[:cfg_b.get("candidati_max", 12)]
    log(f"Candidati: {len(candidati)}")

    for c in candidati:
        try:
            c["notizie"] = notizie(yf.Ticker(c["simbolo"]), 3)
        except Exception:
            c["notizie"] = []

    # contesto indici
    contesto = []
    for voce in universo:
        df = dati.get(voce["simbolo"])
        if voce.get("mercato") != "Indice" or df is None or len(df) < 22:
            continue
        cl = df["Close"]
        contesto.append({
            "nome": voce["nome"],
            "var_1g": num((cl.iloc[-1] / cl.iloc[-2] - 1) * 100, 2),
            "var_5g": num((cl.iloc[-1] / cl.iloc[-6] - 1) * 100, 2),
            "sopra_sma50": bool(cl.iloc[-1] > cl.rolling(50).mean().iloc[-1]),
        })

    # 3. filtro AI
    stats_prima = statistiche(idee)
    ai = filtro_claude(cfg_b, candidati, contesto, stats_prima)
    giudizi = {g["id"]: g for g in (ai or {}).get("idee", []) if "id" in g}
    tenute_ok = 0
    for i, c in enumerate(candidati):
        g = giudizi.get(c["id"])
        if ai is None:
            # senza AI: tiene le migliori per punteggio
            c["ai_tenuta"] = None if i < cfg_b.get("idee_max", 5) else False
        else:
            tieni = bool(g and g.get("tenere")) and tenute_ok < cfg_b.get("idee_max", 5)
            c["ai_tenuta"] = tieni
            tenute_ok += tieni
            if g:
                c["fiducia"] = g.get("fiducia")
                c["motivazione"] = g.get("motivazione", "")
                c["rischio"] = g.get("rischio", "")
        c["stato"] = "aperta"
        c["creata"] = ora_iso()

    idee.extend(candidati)
    idee = idee[-3000:]

    # grafici per le idee tenute di oggi e per quelle ancora aperte
    grafici = {}
    for i in idee:
        if i.get("stato") == "aperta" and i.get("ai_tenuta") is not False:
            df = dati.get(i["simbolo"])
            if df is not None:
                grafici[i["simbolo"]] = [[d.strftime("%Y-%m-%d"), pr(v)] for d, v in df["Close"].tail(60).items()]

    salva_json(IDEE, idee)
    salva_json(BREVE, {
        "generato": ora_iso(),
        "commento": (ai or {}).get("commento", ""),
        "modello": cfg_b.get("modello") if ai else None,
        "token": (ai or {}).get("token"),
        "contesto": contesto,
        "idee_oggi": [c["id"] for c in candidati if c["ai_tenuta"] is not False],
        "candidati": len(candidati),
        "universo": len(universo),
        "grafici": grafici,
        "statistiche": statistiche(idee),
    })
    log(f"Fatto: {len(candidati)} candidati, {sum(1 for c in candidati if c['ai_tenuta'] is not False)} idee tenute")


if __name__ == "__main__":
    main()
