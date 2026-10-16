"""
Chiamata unica a Claude per gli agenti:
- risposta strutturata tramite strumento (niente JSON rotto)
- ragionamento esteso adattivo con livello di effort
- registrazione del costo e controllo del budget
- scala di ripiego se l'API rifiuta una combinazione di parametri
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import costi  # noqa: E402
from analisi import log, risposta_strumento  # noqa: E402


def chiama(cfg, modello, sistema, prompt, strumento, voce, max_tokens=16000, effort=None, stima_usd=0.5):
    """Restituisce (dati_strumento, usage) oppure (None, None)."""
    if os.environ.get("SENZA_AI", "").lower() == "true":
        log(f"[{voce}] modalità prova senza AI: nessuna chiamata a Claude")
        return None, None
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log(f"[{voce}] ANTHROPIC_API_KEY mancante")
        return None, None
    if not costi.disponibile(cfg, stima_usd):
        log(f"[{voce}] budget mensile insufficiente (stima {stima_usd:.2f} $): salto")
        return None, None
    import anthropic

    client = anthropic.Anthropic()
    forzato = {"type": "tool", "name": strumento["name"]}
    pensa = {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}} if effort else None
    # ordine dei tentativi: ragionamento + strumento libero (l'API non accetta lo strumento forzato
    # insieme al ragionamento), poi ragionamento + forzato, infine solo strumento forzato
    varianti = ([(pensa, {"type": "auto"}), (pensa, forzato)] if pensa else []) + [(None, forzato)]

    i = 0
    errori_rete = 0
    while i < len(varianti):
        extra, scelta = varianti[i]
        try:
            kw = dict(model=modello, max_tokens=max_tokens, system=sistema,
                      messages=[{"role": "user", "content": prompt}],
                      tools=[strumento], tool_choice=scelta)
            if extra:
                kw["extra_body"] = extra
            # streaming obbligatorio per risposte lunghe (ragionamento esteso): l'SDK rifiuta altrimenti
            with client.messages.stream(**kw) as flusso:
                resp = flusso.get_final_message()
            costi.registra(cfg, modello, resp.usage, voce)
            log(f"[{voce}] {modello}: {resp.usage.input_tokens} token in, {resp.usage.output_tokens} out")
            return risposta_strumento(resp), resp.usage
        except anthropic.BadRequestError as e:
            msg = str(e).lower()
            if "credit" in msg or "billing" in msg:
                log(f"[{voce}] credito API esaurito: {e}")
                return None, None
            log(f"[{voce}] parametri rifiutati ({e}); provo la variante successiva")
            i += 1
        except Exception as e:
            errori_rete += 1
            log(f"[{voce}] errore: {e}")
            if errori_rete >= 2:
                i += 1
                errori_rete = 0
            time.sleep(5)
    return None, None
