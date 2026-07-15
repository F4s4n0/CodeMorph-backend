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

from src.config import WORKSPACE_DIR

logger = logging.getLogger(__name__)


def log_message(session_id, message):
    """Stampa il log sulla console server e lo accoda al file live della sessione."""
    riga = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
    print(riga)

    if not session_id:
        return

    log_dir = WORKSPACE_DIR / str(session_id)
    try:
        os.makedirs(log_dir, exist_ok=True)
        with open(log_dir / "live_logs.txt", "a", encoding="utf-8") as f:
            f.write(riga + "\n")
    except OSError as e:
        # Il log live non deve MAI far fallire la pipeline di migrazione
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

    def annuncia_avvio():
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

            riassunto = getattr(output, "summary", None)
            dettaglio = f": {str(riassunto)[:120]}" if riassunto else "."

            numero = min(indice + 1, len(ruoli))
            log_message(
                session_id,
                f"📈 [{ruolo}] task {numero}/{len(ruoli)} completato{dettaglio}",
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

    return annuncia_avvio, task_callback
