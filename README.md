# Borsa — analisi continua con Claude

GitHub Actions scarica i dati ogni 30 minuti, calcola gli indicatori, chiede a Claude un commento e pubblica tutto sul branch `dati`. La dashboard (`docs/index.html`) legge quei file.

```
config.json                  watchlist, modello, frequenza AI
scripts/analisi.py           dati Yahoo -> indicatori -> Claude -> data.json / storico.json
.github/workflows/analisi.yml  cron + pubblicazione sul branch "dati"
docs/index.html              dashboard
```

## Installazione

1. Crea un repo su GitHub (es. `borsa`) e carica questi file sul branch `main`.
2. **Chiave API**: su console.anthropic.com crea una chiave, poi nel repo vai su
   Settings → Secrets and variables → Actions → New repository secret,
   nome `ANTHROPIC_API_KEY`.
3. **Primo giro**: Actions → *Analisi borsa* → Run workflow. In 1–2 minuti nasce il branch `dati`.
4. **Dashboard**, scegli una strada:
   - *Repo pubblico*: Settings → Pages → Deploy from a branch → `dati` / root.
     La pagina sarà su `https://<utente>.github.io/borsa/`.
   - *Repo privato*: GitHub Pages gratuito non funziona sui repo privati. Apri `index.html`
     da dove vuoi (PC, tuo hosting), poi nelle impostazioni ⚙︎ metti `utente/borsa` e un token.

## Token (per repo privato e pulsante «Analizza ora»)

GitHub → Settings → Developer settings → Fine-grained tokens → solo questo repo, permessi:
- Contents: Read-only
- Actions: Read and write

Il token resta nel localStorage del dispositivo.

## Configurazione (`config.json`)

| Campo | Significato |
|---|---|
| `modello` | `claude-haiku-5-5` (economico) o `claude-sonnet-5-5` (ragiona meglio) |
| `ai_ogni_minuti` | ogni quanto rifare l'analisi AI; i dati si aggiornano comunque a ogni giro |
| `salta_ai_se_invariato` | niente chiamata AI se nessun prezzo è cambiato |
| `titoli` | simboli Yahoo: `.MI` per Piazza Affari, `-USD` per crypto, `^` per indici |
| `mercato` | `Italia`, `USA`, `Crypto`, `Indice` (o quello che vuoi: diventa un filtro) |

## Frequenza

Orari del cron in UTC (vedi `analisi.yml`). Con un repo privato hai 2000 minuti/mese di Actions:
la configurazione attuale ne usa circa 1000–1500 (ogni run conta almeno 1 minuto). Se ti avvicini al limite, passa il cron a `0 7-21 * * 1-5` (ogni ora). Con un repo pubblico sono illimitati.
Il cron di GitHub può ritardare di qualche minuto nelle ore di punta.

## Costi Claude

Una chiamata per analisi con tutti i titoli insieme. Il consumo di token di ogni analisi
è salvato in `data.json` (`ai.token`), così puoi controllarlo; i costi reali li vedi nella console Anthropic.

## Agente breve termine (`scripts/agente_breve.py`, `breve.yml`)

Gira ogni sera feriale alle 22:15 UTC. Ogni giro:
1. **Valuta** le idee aperte con le candele nuove: target, stop o scadenza (default 10 sedute).
   Ingresso simulato all'apertura successiva, senza leva, con `costo_pct` per operazione.
   Se stop e target cadono nella stessa candela, conta lo stop.
2. **Scansiona** `breve.universo` con setup a regole fisse: breakout/breakdown a 20 giorni, pullback nel trend,
   rimbalzo da ipervenduto, storno da ipercomprato. Stop e target sono calcolati sull'ATR.
3. **Claude** (`breve.modello`) tiene o scarta ogni candidato, senza toccare i livelli.
4. **Registra tutto** in `idee.json`, anche le idee scartate, per misurare se il filtro AI aggiunge valore.

Parametri in `config.json → breve`: `idee_max`, `candidati_max`, `rr_minimo`, `giorni_max`, `costo_pct`, `universo`.
Un'idea resta "da guardare" finché non ci sono almeno 30-50 operazioni chiuse.
