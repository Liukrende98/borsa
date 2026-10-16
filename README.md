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

Gira ogni sera feriale alle 22:15 UTC (dura 10-25 minuti). Pipeline a imbuto:

1. **Universo** (`universo.py`, ricostruito ogni 7 giorni): tutte le azioni USA con capitalizzazione > 2 mld $
   dallo screener Nasdaq (~2000), più Italia, Europa, ETF, indici, crypto, valute e materie prime (`liste.json`).
2. **Valutatore**: aggiorna le idee aperte (target, stop, scadenza).
3. **Scanner** su tutto l'universo: 6 setup a regole fisse, filtro di liquidità, punteggio con forza relativa
   (vs S&P 500 / Euro Stoxx 50), forza del settore e regime di mercato (VIX, % titoli sopra la media a 50 giorni).
4. **Filtro trimestrali**: esclude le azioni con risultati attesi dentro la finestra del trade.
5. **Arricchimento** dei 30 finalisti: fondamentali (`arricchisci.py`) e notizie (Yahoo, riserva Google News).
6. **Analista** (`modello_analista`, ragionamento esteso): valuta tutti i finalisti e propone fino a 8 idee.
7. **Critico** (`modello_critico`): avvocato del diavolo, conferma al massimo 5 idee.
8. **Registro**: salva anche le idee scartate e bocciate, per misurare se ogni filtro aggiunge valore.
9. **Retrospettiva** (`retrospettiva.py`, il venerdì): rilegge le idee chiuse, scrive lezioni che l'analista
   usa le sere successive e propone modifiche alle regole, che decidi tu.

## Budget

`budget_mensile_usd` in `config.json` (default 20 $). Ogni chiamata registra il suo costo in `costi.json`;
quando il budget sta per finire gli agenti saltano Claude e usano solo il punteggio dello scanner.
Spesa stimata: circa 0,4 $ a sera per l'agente breve, meno di 2 $ al mese per il monitor.
Per sicurezza imposta anche un limite di spesa nella Console Anthropic.
