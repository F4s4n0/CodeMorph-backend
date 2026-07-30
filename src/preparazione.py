"""
Pre-analisi dello ZIP caricato: seleziona i file che meritano l'analisi.

Su una solution reale arrivano 400-500 file, ma la logica di business vive
in poche decine. Analizzarli tutti costa al cliente senza dargli valore.

Il filtro è a due stadi:
  1. STATICO (gratis): cartelle di build/dipendenze, estensioni non di codice,
     file oltre la dimensione massima, pattern di codice generato. Toglie di
     mezzo i casi certi senza spendere un token.
  2. LLM (pochi centesimi): al modello arrivano SOLO i percorsi, mai il
     contenuto. Riconosce le convenzioni che nessuna lista può prevedere.

L'ultima parola resta all'utente: la lista torna al frontend con i suggerimenti
già applicati, e lui può includere o escludere qualunque file prima di avviare.
"""

import json
import logging
import os
import re

from src.config import DEFAULT_ESCLUSIONI_PATTERN
from src.llm_config import get_llm

logger = logging.getLogger(__name__)

# Modello usato per la classificazione: legge solo nomi di file, quindi
# un modello economico è più che sufficiente. Configurabile da env.
PRESELEZIONE_PROVIDER = os.getenv("PRESELEZIONE_PROVIDER", "anthropic")
PRESELEZIONE_MODEL = os.getenv("PRESELEZIONE_MODEL", "claude-haiku-4-5-20251001")

# Oltre questo numero di file la classificazione LLM viene saltata: la lista
# sarebbe troppo lunga e i filtri statici hanno già fatto il grosso.
MAX_FILE_PER_LLM = 800


def _escluso_da_pattern(percorso_relativo):
    """
    Esclusione statica per pattern di nome: codice generato, minificati,
    lock file. Ritorna il motivo, oppure None se il file va tenuto.
    """
    nome = percorso_relativo.lower().replace("\\", "/")
    for pattern, motivo in DEFAULT_ESCLUSIONI_PATTERN:
        if nome.endswith(pattern) or f"/{pattern}" in nome or nome == pattern:
            return motivo
    return None


def _prompt_classificazione(percorsi):
    elenco = "\n".join(percorsi)
    return (
        "Ecco l'elenco dei file di un progetto software legacy da modernizzare.\n"
        "Il tuo compito è indicare quali file NON contengono logica di business "
        "rilevante per l'analisi di migrazione, e possono quindi essere esclusi "
        "per non far sprecare tempo e denaro al cliente.\n\n"
        "Considera tipicamente ESCLUDIBILI:\n"
        "- codice generato automaticamente da tool o designer\n"
        "- file di configurazione di progetto, IDE, build e packaging\n"
        "- risorse, asset, traduzioni, file di localizzazione\n"
        "- librerie di terze parti incluse nel repository\n"
        "- test automatici e file di esempio o demo\n"
        "- boilerplate senza logica (entry point vuoti, costanti isolate)\n\n"
        "Considera SEMPRE DA INCLUDERE tutto ciò che può contenere regole di "
        "business, accesso ai dati, interfacce utente principali o procedure.\n"
        "Nel dubbio, INCLUDI il file: escludere per errore è più dannoso che "
        "analizzare qualche file in più.\n\n"
        "ELENCO FILE:\n"
        f"{elenco}\n\n"
        "Rispondi ESCLUSIVAMENTE con un oggetto JSON valido, senza testo prima "
        "o dopo, senza blocchi di codice markdown, in questo formato:\n"
        '{"escludi": [{"file": "percorso/esatto/come/in/elenco", "motivo": "spiegazione breve in italiano"}]}\n'
        "Se ritieni che tutti i file siano rilevanti, rispondi: {\"escludi\": []}"
    )


def _classifica_con_llm(percorsi):
    """
    Chiede al modello quali file escludere. Ritorna {percorso: motivo}.
    In caso di errore ritorna un dizionario vuoto: la pre-analisi è un aiuto,
    non deve mai impedire di procedere.
    """
    if not percorsi:
        return {}
    if len(percorsi) > MAX_FILE_PER_LLM:
        logger.info("Classificazione LLM saltata: %d file oltre il limite di %d",
                    len(percorsi), MAX_FILE_PER_LLM)
        return {}

    try:
        llm = get_llm(provider=PRESELEZIONE_PROVIDER, model_name=PRESELEZIONE_MODEL)
        risposta = llm.call(_prompt_classificazione(percorsi))
        testo = risposta if isinstance(risposta, str) else str(risposta)
        # Il modello può incorniciare il JSON in un blocco markdown
        testo = re.sub(r"^```(?:json)?|```$", "", testo.strip(), flags=re.MULTILINE).strip()
        inizio, fine = testo.find("{"), testo.rfind("}")
        if inizio == -1 or fine == -1:
            logger.warning("Classificazione: nessun JSON nella risposta del modello.")
            return {}
        dati = json.loads(testo[inizio:fine + 1])
        esclusi = {}
        for voce in dati.get("escludi", []):
            percorso = (voce.get("file") or "").strip()
            if percorso in percorsi:          # mai fidarsi di percorsi inventati
                esclusi[percorso] = (voce.get("motivo") or "Non rilevante per la migrazione").strip()
        logger.info("Classificazione LLM: %d file suggeriti in esclusione su %d",
                    len(esclusi), len(percorsi))
        return esclusi
    except Exception:
        logger.exception("Classificazione LLM fallita: procedo senza suggerimenti.")
        return {}


def analizza_sorgenti(cartella_sorgenti, escludi_cartelle, estensioni_valide, max_file_size):
    """
    Costruisce l'elenco dei file candidati all'analisi.

    Ritorna una lista di dizionari:
      {"file": percorso relativo, "dimensione": byte, "incluso": bool, "motivo": str|None}

    I file scartati dai limiti tecnici (binari, cartelle di dipendenze) NON
    compaiono affatto. Compaiono invece quelli esclusi dai pattern o dall'LLM,
    deselezionati e con il motivo, così l'utente può sempre recuperarli.
    """
    candidati = []
    for root, dirs, files in os.walk(cartella_sorgenti):
        dirs[:] = [d for d in dirs if d not in escludi_cartelle]
        for nome_file in files:
            percorso_assoluto = os.path.join(root, nome_file)
            relativo = os.path.relpath(percorso_assoluto, cartella_sorgenti).replace("\\", "/")

            if os.path.splitext(nome_file)[1].lower() not in estensioni_valide:
                continue
            try:
                dimensione = os.path.getsize(percorso_assoluto)
            except OSError:
                continue
            if dimensione > max_file_size:
                continue

            motivo_pattern = _escluso_da_pattern(relativo)
            candidati.append({
                "file": relativo,
                "dimensione": dimensione,
                "incluso": motivo_pattern is None,
                "motivo": motivo_pattern,
            })

    # Alla classificazione LLM vanno solo i file sopravvissuti ai filtri statici
    da_classificare = [c["file"] for c in candidati if c["incluso"]]
    esclusi_llm = _classifica_con_llm(da_classificare)
    for c in candidati:
        if c["file"] in esclusi_llm:
            c["incluso"] = False
            c["motivo"] = esclusi_llm[c["file"]]

    candidati.sort(key=lambda c: (not c["incluso"], c["file"].lower()))
    return candidati
