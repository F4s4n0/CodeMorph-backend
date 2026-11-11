"""
Rileva le risposte del modello INTERROTTE dal limite di lunghezza.

Ogni documento di fase e' scritto in una sola risposta, e ogni risposta ha un
tetto (LLM_MAX_TOKENS). Quando il documento lo supera il provider interrompe
la generazione a meta' frase e lo dichiara nel motivo di arresto
("length"). CrewAI restituisce solo il testo e scarta quel dato: il documento
tagliato veniva salvato, passato agli agenti successivi e consegnato al
cliente senza che nessuno lo sapesse.

Qui il motivo di arresto viene letto alla fonte, avvolgendo
`litellm.completion` (il punto da cui passano tutte le chiamate di CrewAI).
Il modulo NON cambia la risposta e non interviene sul flusso: osserva e
registra. Chi coordina i task (src/live_log.py) chiede poi se l'ULTIMA
risposta del task appena concluso era stata interrotta.

Perche' l'ultima: se una risposta tagliata risulta inutilizzabile CrewAI
richiama il modello, e conta l'esito della chiamata che ha prodotto il
documento, non dei tentativi precedenti.

Le chiamate sono registrate per THREAD: una fase gira per intero nel thread
che ha chiamato kickoff(), quindi sessioni diverse non si mescolano.
"""

import logging
import threading

logger = logging.getLogger(__name__)

# LiteLLM riconduce i motivi dei vari provider a "length"; gli altri valori
# sono qui per prudenza, nel caso un provider non venga normalizzato.
_MOTIVI_LIMITE = {"length", "max_tokens", "max_output_tokens"}

# Oltre questo numero di chiamate non lette si scartano le piu' vecchie:
# alcune crew (micro-agenti del grafo) non hanno nessuno che le consulti.
_MAX_CHIAMATE_IN_MEMORIA = 200

_locale = threading.local()
_blocco_installazione = threading.Lock()
_installato = False


def _chiamate():
    lista = getattr(_locale, "chiamate", None)
    if lista is None:
        lista = _locale.chiamate = []
    return lista


def _registra(risposta):
    scelte = getattr(risposta, "choices", None)
    if not scelte:
        return          # risposta in streaming o di forma inattesa: si ignora
    motivo = str(getattr(scelte[0], "finish_reason", "") or "").lower()
    uso = getattr(risposta, "usage", None)
    chiamate = _chiamate()
    chiamate.append({
        "interrotta": motivo in _MOTIVI_LIMITE,
        "token_output": getattr(uso, "completion_tokens", None),
    })
    if len(chiamate) > _MAX_CHIAMATE_IN_MEMORIA:
        del chiamate[:-_MAX_CHIAMATE_IN_MEMORIA]


def installa_controllo_troncamento():
    """
    Avvolge litellm.completion per leggere il motivo di arresto. Idempotente.

    Non solleva mai: se LiteLLM non e' disponibile o cambia forma, la
    piattaforma lavora come prima, semplicemente senza questo controllo.
    """
    global _installato
    with _blocco_installazione:
        if _installato:
            return True
        try:
            import litellm

            originale = litellm.completion

            def completion_osservata(*args, **kwargs):
                risposta = originale(*args, **kwargs)
                try:
                    _registra(risposta)
                except Exception:
                    pass      # l'osservazione non deve mai rompere una chiamata
                return risposta

            completion_osservata.__wrapped__ = originale
            litellm.completion = completion_osservata
            _installato = True
            return True
        except Exception as e:
            logger.warning("Controllo dei documenti interrotti non attivo: %s", e)
            return False


def azzera_chiamate():
    """Dimentica le chiamate registrate finora in questo thread."""
    _locale.chiamate = []


def ultima_risposta_interrotta():
    """
    Esito dell'ULTIMA chiamata al modello fatta in questo thread dall'ultimo
    azzeramento, poi azzera. Ritorna None se non interrotta (o se non risulta
    alcuna chiamata), altrimenti un dizionario con `token_output`.
    """
    chiamate = _chiamate()
    ultima = chiamate[-1] if chiamate else None
    _locale.chiamate = []
    if ultima and ultima["interrotta"]:
        return {"token_output": ultima["token_output"]}
    return None
