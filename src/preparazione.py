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

from src.config import DEFAULT_ESCLUSIONI_PATTERN, MODELLO_PREDEFINITO
from src.llm_config import get_llm

logger = logging.getLogger(__name__)

# Ripiego quando il chiamante non indica provider e modello: la scelta
# dell'utente ha sempre la precedenza, cosi' l'intera sessione gira sul
# modello che ha selezionato e non su uno diverso a sua insaputa.
PRESELEZIONE_PROVIDER = os.getenv("PRESELEZIONE_PROVIDER", "anthropic")
PRESELEZIONE_MODEL = os.getenv("PRESELEZIONE_MODEL", MODELLO_PREDEFINITO)

# Oltre questo numero di file la classificazione LLM viene saltata: la lista
# sarebbe troppo lunga e i filtri statici hanno già fatto il grosso.
MAX_FILE_PER_LLM = 800

# Formati binari con un estrattore dedicato: NON vanno trattati come
# illeggibili. Un .scx è un DBF binario, ma il parser FoxPro ne ricava
# metodi e proprietà: escluderlo perché "binario" toglierebbe al cliente
# proprio i file dove sta la logica.
ESTENSIONI_CON_ESTRATTORE = {".scx", ".vcx", ".mnx", ".dbf"}

# Quanto si legge per capire se un file è testo. 4 KB bastano: un binario
# mostra byte nulli quasi subito.
CAMPIONE_BINARIO = 4096


def _sembra_binario(percorso, soglia=0.15):
    """
    True se il file non è leggibile come testo.

    Alcune estensioni ammesse esistono in DUE varianti: un .dfm Delphi può
    essere salvato come testo o come binario, e dall'estensione non si può
    sapere. Passare un binario agli agenti significa riempire il contesto di
    caratteri illeggibili — peggio che non leggerlo, perché la documentazione
    verrebbe costruita su quella spazzatura senza che nessuno se ne accorga.
    """
    try:
        with open(percorso, "rb") as f:
            campione = f.read(CAMPIONE_BINARIO)
    except OSError:
        return False
    if not campione:
        return False
    if b"\x00" in campione:
        return True
    controllo = sum(1 for b in campione if b < 32 and b not in (9, 10, 13))
    return (controllo / len(campione)) > soglia


def _e_testo_leggibile(percorso):
    """True se un file con estensione sconosciuta sembra comunque codice."""
    return not _sembra_binario(percorso)


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
        "business, accesso ai dati, interfacce utente principali o procedure.\n\n"
        "ATTENZIONE ALLE TABELLE DI DATI (.dbf, .db, .mdb e simili): NON vanno "
        "escluse. Non ne leggiamo i dati — che sarebbero inutili e pesanti — ma "
        "ne estraiamo lo SCHEMA: nomi delle colonne, tipi, chiavi. E' la base su "
        "cui viene progettato il database di destinazione: senza, il nuovo schema "
        "verrebbe inventato invece che derivato da quello esistente. Escludile "
        "SOLO se il nome indica chiaramente un file temporaneo o di scarto "
        "(tmp_, _bak, test_, copia di...).\n\n"
        "Nel dubbio, INCLUDI il file: escludere per errore è più dannoso che "
        "analizzare qualche file in più. Un file escluso per sbaglio e' un "
        "pezzo di sistema che non verra' documentato, e il cliente non potra' "
        "accorgersene leggendo i documenti.\n\n"
        "ELENCO FILE:\n"
        f"{elenco}\n\n"
        "Rispondi ESCLUSIVAMENTE con un oggetto JSON valido, senza testo prima "
        "o dopo, senza blocchi di codice markdown, in questo formato:\n"
        '{"escludi": [{"file": "percorso/esatto/come/in/elenco", "motivo": "spiegazione breve in italiano"}]}\n'
        "Se ritieni che tutti i file siano rilevanti, rispondi: {\"escludi\": []}"
    )


def _conta_token(modello, testo):
    """
    Stima dei token di un testo. Serve perche' llm.call() restituisce solo la
    stringa: a differenza di una Crew non espone le metriche d'uso, quindi
    senza questo conteggio il consumo della pre-selezione non verrebbe
    addebitato a nessuno.

    Si usa il contatore di LiteLLM, che conosce il tokenizer del modello. Se
    non e' disponibile si ripiega su una stima per caratteri: meglio un
    addebito approssimato per difetto che nessun addebito.
    """
    if not testo:
        return 0
    try:
        import litellm
        return int(litellm.token_counter(model=modello, text=str(testo)))
    except Exception:
        # ~4 caratteri per token: regola grossolana ma dello stesso ordine
        return max(1, len(str(testo)) // 4)


def _classifica_con_llm(percorsi, provider=None, modello=None, tracker=None):
    """
    Chiede al modello quali file escludere. Ritorna {percorso: motivo}.
    In caso di errore ritorna un dizionario vuoto: la pre-analisi è un aiuto,
    non deve mai impedire di procedere.

    `provider` e `modello` sono quelli scelti dall'utente per la sessione: se
    mancano si ricade sui valori d'ambiente.
    """
    if not percorsi:
        return {}
    if len(percorsi) > MAX_FILE_PER_LLM:
        logger.info("Classificazione LLM saltata: %d file oltre il limite di %d",
                    len(percorsi), MAX_FILE_PER_LLM)
        return {}

    try:
        prov = provider or PRESELEZIONE_PROVIDER
        mod = modello or PRESELEZIONE_MODEL
        logger.info("Classificazione file con %s / %s su %d percorsi.", prov, mod, len(percorsi))
        llm = get_llm(provider=prov, model_name=mod)
        prompt = _prompt_classificazione(percorsi)
        risposta = llm.call(prompt)
        testo = risposta if isinstance(risposta, str) else str(risposta)

        # Il consumo va addebitato come quello delle fasi: e' una chiamata
        # LLM pagata al provider come tutte le altre.
        if tracker is not None:
            p_tok = _conta_token(mod, prompt)
            c_tok = _conta_token(mod, testo)
            tracker.aggiungi_metriche({
                "prompt_tokens": p_tok,
                "completion_tokens": c_tok,
                "total_tokens": p_tok + c_tok,
                "successful_requests": 1,
            })
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


def analizza_sorgenti(cartella_sorgenti, escludi_cartelle, estensioni_valide, max_file_size,
                      provider=None, modello=None, tracker=None):
    """
    Costruisce l'elenco dei file candidati all'analisi.

    Ritorna una lista di dizionari:
      {"file": percorso relativo, "dimensione": byte, "incluso": bool, "motivo": str|None}

    OGNI file trovato riceve una decisione VISIBILE, con il suo motivo:
    pattern noto, suggerimento dell'LLM, formato binario, dimensione oltre il
    limite, estensione non riconosciuta. Il cliente li vede deselezionati e
    può includerli comunque.

    Restano fuori dall'elenco solo i file che nessuno vorrebbe analizzare
    (immagini, eseguibili, archivi) e le cartelle di build o dipendenze:
    elencarli sarebbe rumore, non informazione.

    Il principio: il cliente deve poter sapere COSA verrà analizzato e cosa
    no. Un file che sparisce in silenzio e' un pezzo di sistema che non viene
    documentato senza che nessuno se ne accorga.
    """
    candidati = []
    ignorati = 0
    for root, dirs, files in os.walk(cartella_sorgenti):
        dirs[:] = [d for d in dirs if d not in escludi_cartelle]
        for nome_file in files:
            percorso_assoluto = os.path.join(root, nome_file)
            relativo = os.path.relpath(percorso_assoluto, cartella_sorgenti).replace("\\", "/")
            estensione = os.path.splitext(nome_file)[1].lower()

            try:
                dimensione = os.path.getsize(percorso_assoluto)
            except OSError:
                continue

            # --- Estensione NON riconosciuta ------------------------------
            # Prima sparivano in silenzio. Se il contenuto è testo può essere
            # codice in un formato che non conosciamo: si mostra deselezionato
            # e il cliente decide. Se è binario (immagini, eseguibili, archivi)
            # resta fuori: elencarlo sarebbe solo rumore.
            if estensione not in estensioni_valide:
                if dimensione <= max_file_size and _e_testo_leggibile(percorso_assoluto):
                    candidati.append({
                        "file": relativo,
                        "dimensione": dimensione,
                        "incluso": False,
                        "motivo": "Estensione non riconosciuta: il contenuto sembra testo, "
                                  "includilo se contiene codice.",
                    })
                else:
                    ignorati += 1
                continue

            # --- Troppo grande --------------------------------------------
            # Anche questo spariva senza dire nulla, e su un file di logica
            # importante il cliente non poteva nemmeno saperlo.
            if dimensione > max_file_size:
                candidati.append({
                    "file": relativo,
                    "dimensione": dimensione,
                    "incluso": False,
                    "motivo": f"Troppo grande ({dimensione // 1024} KB): oltre il limite "
                              f"di {max_file_size // 1024} KB per singolo file.",
                })
                continue

            # --- Binario in un'estensione attesa come testo ----------------
            # I formati FoxPro con estrattore dedicato sono binari per natura
            # e vanno lasciati passare.
            if estensione not in ESTENSIONI_CON_ESTRATTORE and _sembra_binario(percorso_assoluto):
                candidati.append({
                    "file": relativo,
                    "dimensione": dimensione,
                    "incluso": False,
                    "motivo": "Formato binario: il contenuto non è leggibile come testo "
                              "e non produrrebbe analisi utile.",
                })
                continue

            motivo_pattern = _escluso_da_pattern(relativo)
            candidati.append({
                "file": relativo,
                "dimensione": dimensione,
                "incluso": motivo_pattern is None,
                "motivo": motivo_pattern,
            })

    if ignorati:
        logger.info("Pre-analisi: %d file non elencati (binari o troppo grandi "
                    "con estensione sconosciuta).", ignorati)

    # Alla classificazione LLM vanno solo i file sopravvissuti ai filtri statici
    da_classificare = [c["file"] for c in candidati if c["incluso"]]
    esclusi_llm = _classifica_con_llm(da_classificare, provider=provider,
                                      modello=modello, tracker=tracker)
    for c in candidati:
        if c["file"] in esclusi_llm:
            c["incluso"] = False
            c["motivo"] = esclusi_llm[c["file"]]

    candidati.sort(key=lambda c: (not c["incluso"], c["file"].lower()))
    return candidati
