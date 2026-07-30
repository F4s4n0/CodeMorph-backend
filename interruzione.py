"""
Interruzione cooperativa delle fasi in corso.

Un thread Python non si può terminare dall'esterno: l'unica via pulita è che
il lavoro controlli periodicamente un flag e si fermi da sé. Questo modulo
tiene il registro delle richieste di stop.

Il registro è IN MEMORIA (set + lock): il task in background gira nello stesso
processo che riceve la richiesta di stop, quindi non serve passare dal database
per la comunicazione — e un controllo in memoria si può fare a ogni passo
dell'agente senza costi.

Lo stato su Supabase viene comunque aggiornato da main.py, per informare il
frontend anche dopo un ricaricamento della pagina.
"""

import logging
import threading

logger = logging.getLogger(__name__)

_stop_richiesti = set()
_lock = threading.Lock()


class FaseInterrotta(Exception):
    """Sollevata quando l'utente ha chiesto l'interruzione della fase."""
    pass


def richiedi_stop(session_id):
    """Registra la richiesta di interruzione per una sessione."""
    with _lock:
        _stop_richiesti.add(session_id)
    logger.info("Richiesta di stop registrata per la sessione %s", session_id)


def stop_richiesto(session_id):
    """True se per questa sessione è stata chiesta l'interruzione."""
    if not session_id:
        return False
    with _lock:
        return session_id in _stop_richiesti


def pulisci(session_id):
    """
    Rimuove la richiesta dal registro: da chiamare quando la fase termina
    (interrotta o completata), altrimenti un rilancio partirebbe già "fermato".
    """
    with _lock:
        _stop_richiesti.discard(session_id)


def verifica_stop(session_id):
    """
    Punto di controllo: solleva FaseInterrotta se l'utente ha chiesto lo stop.
    Da chiamare nei punti sicuri del lavoro (tra un file e l'altro, a ogni
    passo dell'agente), MAI dentro una scrittura a metà.
    """
    if stop_richiesto(session_id):
        raise FaseInterrotta("Elaborazione interrotta su richiesta dell'utente.")
