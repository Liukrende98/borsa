#!/usr/bin/env python3
"""
Agente breve termine (1-10 sedute) - versione "imbuto".

Ogni sera feriale dopo la chiusura USA:
0. RETROSPETTIVA (una volta a settimana): Claude rilegge le idee chiuse e scrive lezioni
1. VALUTA le idee aperte con le candele nuove (target / stop / scadenza)
2. SCARICA l'universo: ~2000 azioni USA, Italia, Europa, ETF, indici, crypto, valute, materie prime
3. SCANSIONA tutto con setup a regole fisse + forza relativa, settore, regime di mercato, liquidità
4. FILTRA i migliori: niente trimestrali dentro la finestra del trade
5. ARRICCHISCE i finalisti: fondamentali e notizie
6. ANALISTA (Claude, ragionamento esteso) sceglie la rosa
7. CRITICO (Claude, modello più forte) fa l'avvocato del diavolo: restano solo le idee che reggono
8. REGISTRA tutto, scartate comprese, per misurare se ogni filtro aggiunge valore

Simulazione: ingresso all'apertura successiva, senza leva, costo fisso per operazione.
"""
import json
import math
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analisi import num, carica_json, salva_json, ora_iso, log, rsi, notizie, query_notizie  # noqa: E402
import ai  # noqa: E402
import arricchisci  # noqa: E402
import costi  # noqa: E402
import retrospettiva  # noqa: E402
import universo  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.json"
OUT_DIR = ROOT / "docs"
IDEE = OUT_DIR / "idee.json"
VISTA = OUT_DIR / "idee_vista.json"
BREVE = OUT_DIR / "breve.json"

CHIUSE = ("target", "stop", "scaduta")
BENCHMARK = ["^GSPC", "^VIX", "^STOXX50E"] + sorted(set(universo.SETTORI_ETF.values()))


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

def scarica_tutti(simboli, blocco=100):
    """Scarica 1 anno di candele giornaliere a blocchi; restituisce {simbolo: DataFrame}."""
    dati = {}
    for i in range(0, len(simboli), blocco):
        parte = simboli[i:i + blocco]
        df = None
        for t in range(3):
            try:
                df = yf.download(parte, period="1y", interval="1d", group_by="ticker",
                                 auto_adjust=True, threads=True, progress=False)
                break
            except Exception as e:
                log(f"  blocco {i // blocco + 1}: tentativo {t + 1} fallito ({e})")
                time.sleep(5 * (t + 1))
        if df is None or df.empty:
            continue
        multi = isinstance(df.columns, pd.MultiIndex)
        livello0 = set(df.columns.get_level_values(0)) if multi else set()
        for s in parte:
            try:
                sub = df[s] if multi and s in livello0 else (df if not multi and len(parte) == 1 else None)
                if sub is None:
                    continue
                sub = sub.dropna(subset=["Open", "High", "Low", "Close"])
                if len(sub) >= 30:
                    dati[s] = sub
            except Exception:
                pass
        log(f"  scaricati {min(i + blocco, len(simboli))}/{len(simboli)} (validi {len(dati)})")
        time.sleep(1)
    return dati


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


def var_pct(df, n):
    if df is None or len(df) <= n:
        return None
    c = df["Close"]
    return (float(c.iloc[-1]) / float(c.iloc[-1 - n]) - 1) * 100


def scambi_medi(df):
    """Controvalore medio giornaliero a 20 sedute (prezzo × volume)."""
    if "Volume" not in df:
        return None
    x = (df["Close"] * df["Volume"].fillna(0)).tail(20).mean()
    return float(x) if x == x else None


def regime(dati, voci):
    out = {}
    spx = dati.get("^GSPC")
    if spx is not None and len(spx) > 200:
        c = spx["Close"]
        out["spx_sopra_sma200"] = bool(c.iloc[-1] > c.rolling(200).mean().iloc[-1])
        out["spx_sopra_sma50"] = bool(c.iloc[-1] > c.rolling(50).mean().iloc[-1])
        out["spx_var_1m"] = num(var_pct(spx, 21), 2)
    vix = dati.get("^VIX")
    if vix is not None and len(vix):
        out["vix"] = num(vix["Close"].iloc[-1], 1)

    def ampiezza(mercati):
        dfs = [dati[v["simbolo"]] for v in voci if v["mercato"] in mercati and v["simbolo"] in dati]
        dfs = [d for d in dfs if len(d) >= 50]
        if not dfs:
            return None
        sopra = sum(1 for d in dfs if d["Close"].iloc[-1] > d["Close"].tail(50).mean())
        return num(sopra / len(dfs) * 100, 1)

    out["ampiezza_usa"] = ampiezza(("USA",))
    out["ampiezza_europa"] = ampiezza(("Italia", "Europa"))
    amp = out.get("ampiezza_usa") or 50
    if out.get("spx_sopra_sma200") and amp >= 55:
        out["etichetta"] = "rialzista"
    elif out.get("spx_sopra_sma200") is False and amp <= 45:
        out["etichetta"] = "ribassista"
    else:
        out["etichetta"] = "incerto"
    out["volatilita_alta"] = bool((out.get("vix") or 0) >= 25)
    return out


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
    sc_an = [i for i in chiuse if i.get("ai_tenuta") is False and i.get("fase_scarto", 1) == 1]
    sc_cr = [i for i in chiuse if i.get("ai_tenuta") is False and i.get("fase_scarto") == 2]

    def per(chiave):
        return {k: aggrega([i for i in tenute if (i.get(chiave) or "n/d") == k])
                for k in sorted({i.get(chiave) or "n/d" for i in tenute})}

    return {
        "tenute": aggrega(tenute),
        "scartate": aggrega(sc_an + sc_cr),
        "scartate_analista": aggrega(sc_an),
        "scartate_critico": aggrega(sc_cr),
        "per_setup": per("setup"),
        "per_mercato": per("mercato"),
        "per_regime": per("regime"),
        "aperte": sum(1 for i in idee if i.get("stato") == "aperta" and i.get("ai_tenuta") is not False),
    }


# -------------------------------------------------------------------- AI

STRUMENTO_ANALISTA = {
    "name": "salva_selezione",
    "description": "Salva la valutazione di tutti i candidati.",
    "input_schema": {
        "type": "object",
        "properties": {
            "commento": {"type": "string", "description": "3-5 frasi: contesto di mercato e criterio di scelta"},
            "idee": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "tenere": {"type": "boolean", "description": "true = passa al comitato"},
                        "fiducia": {"type": "integer", "minimum": 1, "maximum": 5},
                        "motivazione": {"type": "string", "description": "1-3 frasi: la tesi"},
                        "rischio": {"type": "string", "description": "1 frase: il rischio principale"},
                    },
                    "required": ["id", "tenere", "fiducia", "motivazione", "rischio"],
                },
            },
        },
        "required": ["commento", "idee"],
    },
}

SISTEMA_ANALISTA = """Sei un trader professionista di breve termine (1-10 sedute) su azioni USA ed europee, ETF, indici, crypto, valute e materie prime.
Uno scanner quantitativo ha già esaminato migliaia di strumenti e ti passa i finalisti con indicatori, forza relativa, settore, fondamentali, notizie e regime di mercato.
Ingresso, stop e target sono calcolati dal codice: non modificarli e non inventare numeri.

Come valutare:
- Allineamento: il trade va nella direzione del regime, del settore e della forza relativa? Un long su un titolo debole in un settore debole in un mercato ribassista è da scartare.
- Qualità del setup: volumi, posizione rispetto alle medie, distanza dai massimi/minimi, volatilità (ATR) in rapporto allo stop.
- Catalizzatori e rischi nelle notizie: un movimento spiegato da una notizia forte vale più di uno casuale; notizie negative contro un long sono un campanello d'allarme.
- Fondamentali come filtro: evita long su società in forte deterioramento, evita short su titoli con short interest altissimo (rischio squeeze).
- Diversificazione: non tenere più idee che sono la stessa scommessa (stesso settore, stessa direzione, stesso fattore).
- Trimestrale: se è "SCONOSCIUTA" per un'azione, c'è il rischio di un gap sui risultati dentro la finestra: abbassa la fiducia o scarta.
- Lezioni e statistiche storiche: applicale se poggiano su abbastanza casi.
Sii selettivo: meglio zero idee che idee mediocri.
Italiano, numeri con la virgola decimale. Rispondi compilando lo strumento salva_selezione con TUTTI i candidati."""

STRUMENTO_CRITICO = {
    "name": "salva_verdetto",
    "description": "Salva il verdetto finale su ciascuna idea.",
    "input_schema": {
        "type": "object",
        "properties": {
            "commento": {"type": "string", "description": "2-3 frasi sul giudizio complessivo"},
            "verdetti": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "conferma": {"type": "boolean"},
                        "critica": {"type": "string", "description": "1-3 frasi: il miglior argomento contro, e perché regge o no"},
                        "fiducia_finale": {"type": "integer", "minimum": 1, "maximum": 5},
                    },
                    "required": ["id", "conferma", "critica", "fiducia_finale"],
                },
            },
        },
        "required": ["commento", "verdetti"],
    },
}

SISTEMA_CRITICO = """Sei l'avvocato del diavolo di un comitato di trading. Un analista ha proposto alcune idee di breve termine (1-10 sedute).
Per ciascuna cerca attivamente i motivi concreti per cui fallirà: regime contrario, settore debole, notizie trascurate, stop troppo stretto rispetto alla volatilità, movimento già esaurito, correlazione con altre idee, eventi in arrivo, rischio di gap.
Conferma un'idea solo se regge al tuo miglior argomento contro; altrimenti bocciala. Puoi abbassare o alzare la fiducia.
Usa solo i dati forniti, non inventare numeri. Italiano, virgola decimale. Rispondi compilando lo strumento salva_verdetto."""


def compatta(c, completo=True):
    d = {k: c.get(k) for k in ("id", "simbolo", "nome", "mercato", "settore", "direzione", "setup", "motivo",
                               "prezzo_segnale", "stop", "target", "rr", "rischio_pct", "contesto", "trimestrale")}
    if completo:
        d["fondamentali"] = c.get("fondamentali") or {}
        d["notizie"] = [f'{(n.get("data") or "")[:10]} {n.get("fonte", "")}: {n["titolo"]}' for n in c.get("notizie", [])]
    return d


# ------------------------------------------------------------------- main

def main():
    cfg = carica_json(CONFIG, {})
    cfg_b = cfg.get("breve") or {}
    oggi = date.today()
    idee = carica_json(IDEE, [])
    effort = cfg_b.get("ragionamento", "high")
    prova = os.environ.get("SENZA_AI", "").lower() == "true"
    if prova:
        log("MODALITÀ PROVA: nessuna chiamata a Claude, le nuove idee NON vengono registrate")

    # pulizia: idee nate da run di prova (senza analisi AI) non devono finire nelle statistiche
    prima = len(idee)
    idee = [i for i in idee if not (
        i.get("prova") or
        (i.get("stato") == "aperta" and i.get("ingresso") is None and not i.get("motivazione")
         and i.get("ai_tenuta") is not True and "2026-10-16" <= str(i.get("creata", "")) < "2026-10-16T19:00"))]
    if len(idee) != prima:
        log(f"Rimosse {prima - len(idee)} idee generate da run di prova")

    # 0. retrospettiva settimanale
    if oggi.weekday() == cfg_b.get("giorno_retrospettiva", 4) and not prova:
        lezioni = retrospettiva.esegui(cfg, idee)
    else:
        lezioni = carica_json(retrospettiva.LEZIONI, {})

    # 1-2. universo e dati
    voci = universo.costruisci(cfg_b)
    per_simbolo = {v["simbolo"]: v for v in voci}
    aperte = [i for i in idee if i.get("stato") == "aperta"]
    simboli = list(dict.fromkeys([v["simbolo"] for v in voci] + BENCHMARK + [i["simbolo"] for i in aperte]))
    log(f"Scarico {len(simboli)} strumenti")
    dati = scarica_tutti(simboli, cfg_b.get("blocco_download", 100))
    log(f"Dati validi: {len(dati)}/{len(simboli)}")

    # valutazione idee aperte
    for idea in aperte:
        valuta(idea, dati.get(idea["simbolo"]), cfg_b)

    reg = regime(dati, voci)
    log(f"Regime: {reg}")

    # 3. scansione
    occupati = {i["simbolo"] for i in idee if i.get("stato") == "aperta"}
    gia_visti = {i["id"] for i in idee}
    var63 = {s: var_pct(dati.get(s), 63) for s in BENCHMARK}
    candidati, scartati_liquidita = [], 0
    for v in voci:
        s = v["simbolo"]
        df = dati.get(s)
        if df is None or s in occupati:
            continue
        m = v["mercato"]
        if m in ("USA", "ETF", "Italia", "Europa"):
            soglia = cfg_b.get("liquidita_min_usd", 5e6) if m in ("USA", "ETF") else cfg_b.get("liquidita_min_eur", 2e6)
            sm = scambi_medi(df)
            if sm is not None and sm < soglia:
                scartati_liquidita += 1
                continue
        try:
            trovati = scansiona(v, df, cfg_b)
        except Exception as e:
            log(f"  {s}: errore nello scanner ({e})")
            continue
        if not trovati:
            continue
        bench = "^GSPC" if m in ("USA", "ETF") else "^STOXX50E" if m in ("Italia", "Europa") else None
        v63 = var_pct(df, 63)
        rs = (v63 - var63[bench]) if bench and v63 is not None and var63.get(bench) is not None else None
        etf = universo.SETTORI_ETF.get(v.get("settore", "")) if m == "USA" else None
        rs_sett = (var63[etf] - var63["^GSPC"]) if etf and var63.get(etf) is not None and var63.get("^GSPC") is not None else None
        for c in trovati:
            if c["id"] in gia_visti:
                continue
            d = 1 if c["direzione"] == "long" else -1
            bonus = 0.0
            if rs is not None:
                bonus += max(-1.0, min(1.0, d * rs / 10))
            if rs_sett is not None:
                bonus += max(-0.5, min(0.5, d * rs_sett / 10))
            if reg["etichetta"] == "rialzista":
                bonus += 0.5 * d
            elif reg["etichetta"] == "ribassista":
                bonus -= 0.5 * d
            c["punteggio"] = num(c["punteggio"] + bonus, 2)
            c["settore"] = v.get("settore", "")
            c["regime"] = reg["etichetta"]
            c["contesto"]["forza_relativa_3m"] = num(rs, 2)
            c["contesto"]["forza_settore_3m"] = num(rs_sett, 2)
            candidati.append(c)
    candidati.sort(key=lambda c: c["punteggio"], reverse=True)
    trovati_tot = len(candidati)
    log(f"Candidati dallo scanner: {trovati_tot} (esclusi per liquidità: {scartati_liquidita})")

    # 4. filtro trimestrali sui migliori
    finalisti, esclusi_trim = [], 0
    finestra = int(cfg_b.get("giorni_max", 10) * 1.5)
    for c in candidati[:cfg_b.get("candidati_scan", 60)]:
        if c["mercato"] in arricchisci.AZIONI:
            t, stato = arricchisci.prossima_trimestrale(c["simbolo"], c["mercato"], finestra)
            if t and (t - oggi).days <= finestra:
                esclusi_trim += 1
                continue
            c["trimestrale"] = (t.isoformat() if t else
                                f"nessuna nei prossimi {finestra} giorni" if stato == "nessuna" else
                                "SCONOSCIUTA (non verificabile: trattala come rischio)")
        finalisti.append(c)
        if len(finalisti) >= cfg_b.get("candidati_max", 30):
            break
    log(f"Finalisti: {len(finalisti)} (esclusi per trimestrale vicina: {esclusi_trim})")

    # 5. arricchimento
    for c in finalisti:
        v = per_simbolo.get(c["simbolo"], c)
        if c["mercato"] in arricchisci.AZIONI:
            c["fondamentali"] = arricchisci.fondamentali(c["simbolo"], c["mercato"])
            if not c.get("settore"):
                c["settore"] = c["fondamentali"].get("settore", "")
        try:
            c["notizie"] = notizie(yf.Ticker(c["simbolo"]), 4, query=query_notizie(v))
        except Exception:
            c["notizie"] = []

    # contesto indici
    contesto = []
    for v in voci:
        df = dati.get(v["simbolo"])
        if v["mercato"] != "Indice" or df is None or len(df) < 60:
            continue
        cl = df["Close"]
        contesto.append({"nome": v["nome"], "var_1g": num(var_pct(df, 1), 2), "var_5g": num(var_pct(df, 5), 2),
                         "var_1m": num(var_pct(df, 21), 2), "sopra_sma50": bool(cl.iloc[-1] > cl.tail(50).mean())})

    stats_prima = statistiche(idee)
    base = (f"Data: {oggi.isoformat()}\n"
            f"Regime di mercato: {json.dumps(reg, ensure_ascii=False)}\n"
            f"Indici: {json.dumps(contesto, ensure_ascii=False)}\n"
            f"Lezioni dalla retrospettiva: {json.dumps(lezioni.get('lezioni', []), ensure_ascii=False)}\n"
            f"Statistiche storiche (idee chiuse): {json.dumps({k: stats_prima[k] for k in ('tenute', 'scartate_analista', 'scartate_critico', 'per_setup', 'per_regime')}, ensure_ascii=False)}\n")

    # 6. analista
    shortlist_max = cfg_b.get("shortlist_max", 8)
    idee_max = cfg_b.get("idee_max", 5)
    sel, commento, commento_critico = None, "", ""
    if finalisti:
        prompt = (base + f"\nCandidati ({len(finalisti)}):\n{json.dumps([compatta(c) for c in finalisti], ensure_ascii=False)}\n\n"
                  f"Valuta tutti i candidati. Manda al comitato (tenere=true) al massimo {shortlist_max} idee.")
        sel, _ = ai.chiama(cfg, cfg_b.get("modello_analista", "claude-sonnet-5-5"), SISTEMA_ANALISTA, prompt,
                           STRUMENTO_ANALISTA, "analista", max_tokens=32000, effort=effort, stima_usd=0.6)
    giudizi = {g["id"]: g for g in (sel or {}).get("idee", []) if isinstance(g, dict) and "id" in g}
    commento = (sel or {}).get("commento", "")

    for c in finalisti:
        g = giudizi.get(c["id"])
        if g:
            c.update({"fiducia": g.get("fiducia"), "motivazione": g.get("motivazione", ""), "rischio": g.get("rischio", "")})
    if sel:
        rosa = sorted([c for c in finalisti if giudizi.get(c["id"], {}).get("tenere")],
                      key=lambda c: c.get("fiducia") or 0, reverse=True)[:shortlist_max]
    else:
        rosa = finalisti[:idee_max]
    ids_rosa = {c["id"] for c in rosa}

    # 7. critico
    verdetti = None
    if sel and rosa:
        prompt = (base + f"\nIdee proposte dall'analista ({len(rosa)}):\n"
                  f"{json.dumps([compatta(c) | {'tesi_analista': c.get('motivazione'), 'rischio_analista': c.get('rischio'), 'fiducia_analista': c.get('fiducia')} for c in rosa], ensure_ascii=False)}\n\n"
                  f"Commento dell'analista: {commento}\n"
                  f"Alla fine possono restare al massimo {idee_max} idee.")
        verdetti, _ = ai.chiama(cfg, cfg_b.get("modello_critico", "claude-opus-5-5"), SISTEMA_CRITICO, prompt,
                                STRUMENTO_CRITICO, "critico", max_tokens=24000, effort=effort, stima_usd=0.5)
    ver = {g["id"]: g for g in (verdetti or {}).get("verdetti", []) if isinstance(g, dict) and "id" in g}
    commento_critico = (verdetti or {}).get("commento", "")

    if verdetti:
        for c in rosa:
            g = ver.get(c["id"])
            if g:
                c["critica"] = g.get("critica", "")
                c["fiducia_analista"] = c.get("fiducia")
                c["fiducia"] = g.get("fiducia_finale", c.get("fiducia"))
        finali = sorted([c for c in rosa if ver.get(c["id"], {}).get("conferma")],
                        key=lambda c: c.get("fiducia") or 0, reverse=True)[:idee_max]
    else:
        finali = rosa[:idee_max]
        if sel and rosa:
            commento_critico = "Critico non disponibile: restano le idee migliori dell'analista."
    ids_finali = {c["id"] for c in finali}

    # 8. registrazione (tutti i finalisti, scartati compresi)
    for c in finalisti:
        if not sel:
            c["ai_tenuta"] = None if c["id"] in ids_finali else False
        else:
            c["ai_tenuta"] = c["id"] in ids_finali
        if c["ai_tenuta"] is False:
            c["fase_scarto"] = 2 if c["id"] in ids_rosa else 1
            c.pop("notizie", None)
            c.pop("fondamentali", None)
        c["stato"] = "aperta"
        c["creata"] = ora_iso()
    if not prova:
        idee.extend(finalisti)
    idee = idee[-cfg_b.get("registro_max", 6000):]
    anteprima = [c for c in finalisti if c["id"] in ids_finali] if prova else []

    tenute = [i for i in idee if i.get("ai_tenuta") is not False]
    vista = [i for i in tenute if i.get("stato") == "aperta"] + \
            [i for i in tenute if i.get("stato") != "aperta"][-80:]
    grafici = {}
    for i in vista + anteprima:
        if i.get("stato") == "aperta":
            df = dati.get(i["simbolo"])
            if df is not None:
                grafici[i["simbolo"]] = [[d.strftime("%Y-%m-%d"), pr(x)] for d, x in df["Close"].tail(60).items()]

    conteggi = {}
    for v in voci:
        conteggi[v["mercato"]] = conteggi.get(v["mercato"], 0) + 1

    salva_json(IDEE, idee)
    salva_json(VISTA, vista)
    salva_json(BREVE, {
        "generato": ora_iso(),
        "commento": commento,
        "commento_critico": commento_critico,
        "modelli": {"analista": cfg_b.get("modello_analista") if sel else None,
                    "critico": cfg_b.get("modello_critico") if verdetti else None},
        "regime": reg,
        "contesto": contesto,
        "prova": prova,
        "anteprima": anteprima,
        "idee_oggi": [] if prova else sorted(ids_finali),
        "universo": len(voci),
        "scaricati": len(dati),
        "per_mercato": conteggi,
        "candidati": trovati_tot,
        "esclusi_liquidita": scartati_liquidita,
        "esclusi_trimestrale": esclusi_trim,
        "analizzati": len(finalisti),
        "rosa": len(rosa),
        "grafici": grafici,
        "statistiche": statistiche(idee),
        "costi": costi.carica(),
    })
    log(f"Fatto: {trovati_tot} candidati, {len(finalisti)} analizzati, {len(rosa)} in rosa, {len(finali)} idee finali. "
        f"Spesa API del mese: {costi.carica()['totale_usd']:.2f} $")


if __name__ == "__main__":
    main()
