"""
Log live per sessione: il file che il frontend legge via
GET /api/v1/modernize/logs/{session_id}.

Due garanzie che prima mancavano (ed erano il motivo per cui il frontend
doveva simulare l'avanzamento "a tempo"):

1. Scrittura e lettura usano la STESSA cartella (WORKSPACE_DIR in config):
   prima log_message scriveva nella cartella relativa ./workspace mentre
   l'endpoint leggeva WORKSPACE_DIR, quindi i log reali non arrivavano mai.
2. Le Crew notificano qui l'attività REALE (agente al lavoro, task
   completato) tramite i callback di CrewAI, per tutte e tre le fasi.

Ogni riga è prefissata con l'orario, così il frontend può ancorare la
timeline all'attività effettiva e non a un timer.
"""

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from src.config import WORKSPACE_DIR
from src.maschera_log import maschera_testo
from src.troncamento import azzera_chiamate, ultima_risposta_interrotta

logger = logging.getLogger(__name__)

FUSO_ROMA = ZoneInfo("Europe/Rome")


def log_message(session_id, message):
    """Stampa il log sulla console server e lo accoda al file live della sessione."""
    # Mascherato PRIMA di stampare e di scrivere: print() non passa dal
    # logging, e questo file viene mostrato al cliente nel log live. Un
    # messaggio di errore puo' contenere l'URL della chiamata con la chiave.
    message = maschera_testo(message)
    riga = f"[{datetime.now(FUSO_ROMA).strftime('%H:%M:%S')}] {message}"
    print(riga)

    if not session_id:
        return

    log_dir = WORKSPACE_DIR / str(session_id)
    log_file = log_dir / "live_logs.txt"

    try:
        os.makedirs(log_dir, exist_ok=True)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(riga + "\n")
    except OSError as e:
        logger.warning("Impossibile scrivere il log live di %s: %s", session_id, e)


def _ruolo_del_task(task):
    return getattr(getattr(task, "agent", None), "role", None) or "Agente"


def crea_logger_attivita(session_id, tasks, etichetta=""):
    """
    Ritorna la coppia (annuncia_avvio, task_callback) da agganciare a una
    Crew sequenziale:

    - `annuncia_avvio()` va chiamata subito prima del kickoff e logga il
      primo agente che entra in lavorazione;
    - `task_callback` va passata a Crew(task_callback=...): CrewAI la invoca
      alla fine di OGNI task reale, e qui logghiamo il completamento e
      l'ingresso in lavorazione dell'agente successivo.
    """
    ruoli = [_ruolo_del_task(t) for t in tasks]
    suffisso = f" — {etichetta}" if etichetta else ""
    stato = {"completati": 0}
    # Indici (da 0) dei task la cui risposta e' stata interrotta dal limite
    # di lunghezza: chi salva i documenti li legge da task_callback.task_troncati
    # per scrivere l'avviso anche dentro il deliverable.
    troncati = set()

    def annuncia_avvio():
        # Le chiamate fatte prima di questa crew nello stesso thread (grafo
        # delle dipendenze, sintesi dei contesti) non riguardano i suoi task.
        azzera_chiamate()
        if ruoli:
            log_message(
                session_id,
                f"🧠 [{ruoli[0]}] al lavoro (task 1/{len(ruoli)}){suffisso}...",
            )

    def task_callback(output):
        # Un errore nel logging non deve interrompere la Crew in esecuzione
        try:
            indice = stato["completati"]
            ruolo = getattr(output, "agent", None) \
                or (ruoli[indice] if indice < len(ruoli) else "Agente")

            # NON usare output.summary: in CrewAI e' la DESCRIZIONE del task
            # troncata alle prime parole, non un riassunto del risultato.
            # Ripeteva la consegna gia' stampata all'avvio, facendo sembrare
            # che l'agente ricominciasse invece di aver finito.
            testo = str(getattr(output, "raw", "") or "")
            if testo:
                caratteri = len(testo)
                # Un task "completato" con pochissimo testo e' un fallimento
                # silenzioso: va visto subito nel log, non a valle.
                allarme = " ⚠️ output molto breve" if caratteri < 200 else ""
                dettaglio = f" — {caratteri:,} caratteri prodotti{allarme}".replace(",", ".")
            else:
                dettaglio = " — ⚠️ nessun output prodotto"

            numero = min(indice + 1, len(ruoli))
            log_message(
                session_id,
                f"📈 [{ruolo}] task {numero}/{len(ruoli)} completato{dettaglio}",
            )

            # Un documento tagliato dal limite di lunghezza sembra completo a
            # chi guarda solo il numero di caratteri: va detto esplicitamente.
            interrotta = ultima_risposta_interrotta()
            if interrotta:
                troncati.add(indice)
                token = interrotta.get("token_output")
                misura = f" dopo {token:,} token".replace(",", ".") if token else ""
                log_message(
                    session_id,
                    f"⚠️ [{ruolo}] risposta INTERROTTA dal limite di lunghezza{misura}: "
                    "il risultato e' incompleto, manca la parte finale. Valuta un "
                    "limite di output piu' alto o una selezione di file piu' stretta.",
                )

            stato["completati"] += 1
            prossimo = stato["completati"]
            if prossimo < len(ruoli):
                log_message(
                    session_id,
                    f"🧠 [{ruoli[prossimo]}] al lavoro (task {prossimo + 1}/{len(ruoli)}){suffisso}...",
                )
        except Exception as e:
            logger.warning("Callback di log attività fallito: %s", e)

    task_callback.task_troncati = troncati
    return annuncia_avvio, task_callback
