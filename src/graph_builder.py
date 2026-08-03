import json
import os
import time


import networkx as nx
from crewai import Agent, Task, Crew
from dbfread import DBF  # Libreria nativa per FoxPro

# log_message vive ora in src/live_log.py (stessa cartella di scrittura e
# lettura dei log live). L'import resta qui anche come re-export per il
# codice esistente che lo importava da questo modulo.
import interruzione
from src.config import DELAY_TRA_FILE_SEC, MAX_CARATTERI_SORGENTI
from src.live_log import log_message

ESCLUDI_CARTELLE = {
    # Controllo di versione
    '.git', '.svn', '.hg', 'cvs',
    # Dipendenze
    'node_modules', 'vendor', 'packages', 'bower_components',
    'venv', 'env', '.venv', 'site-packages',
    # Build e artefatti
    'bin', 'obj', 'dist', 'build', 'target', 'out', 'release', 'debug',
    '__pycache__', '.pytest_cache', '.mypy_cache', 'coverage', 'testresults',
    # IDE
    '.idea', '.vscode', '.vs', '__history', '__recovery',
    # Backup tipici dei progetti legacy
    'backup', 'backups', 'old', 'vecchio',
}

# Estensioni dei file che vengono letti e analizzati.
# I formati binari FoxPro (.scx, .dbf) hanno estrattori dedicati; gli altri
# vengono letti come testo. NON aggiungere qui binari senza estrattore
# (.mdb Access, .pbl PowerBuilder): produrrebbero caratteri illeggibili
# che gli agenti analizzerebbero comunque, a spese del cliente.
ESTENSIONI_VALIDE = {
    # --- Visual FoxPro (estrattori nativi per .scx e .dbf) ---
    '.prg', '.scx', '.dbf', '.vcx', '.mnx', '.spr',

    # --- Visual Basic 6 ---
    '.bas', '.frm', '.cls', '.ctl', '.vbp', '.dsr',

    # --- Delphi / Pascal ---
    '.pas', '.dpr', '.dfm', '.dpk', '.inc',

    # --- COBOL e mainframe ---
    '.cbl', '.cob', '.cpy', '.jcl', '.pco', '.ddl',

    # --- AS/400 - RPG ---
    '.rpg', '.rpgle', '.sqlrpgle', '.clp', '.clle', '.dds',

    # --- Altri legacy ---
    '.f', '.f77', '.f90', '.for',           # Fortran
    '.asm', '.s',                            # Assembly
    '.pl', '.pm',                            # Perl
    '.tcl', '.awk',                          # scripting legacy
    '.4gl', '.per',                          # Informix 4GL
    '.p', '.w', '.i',                        # Progress OpenEdge
    '.abap',                                 # SAP ABAP
    '.vb',                                   # VB.NET

    # --- Linguaggi moderni (sistemi misti e stack target) ---
    '.cs', '.java', '.py', '.js', '.ts', '.jsx', '.tsx',
    '.php', '.go', '.rs', '.rb', '.kt', '.swift', '.scala',
    '.c', '.cpp', '.cc', '.cxx', '.h', '.hpp',

    # --- Dati, configurazione, markup ---
    '.sql', '.json', '.xml', '.yaml', '.yml', '.ini', '.conf', '.config',
    '.html', '.htm', '.css', '.scss',
    '.asp', '.aspx', '.ascx', '.jsp',        # pagine server-side legacy
    '.bat', '.cmd', '.sh', '.ps1',           # script di lancio e job
}

MAX_FILE_SIZE = 250 * 1024


# =====================================================================
# Estrazione nativa FoxPro
# =====================================================================

def extract_foxpro_scx_code(file_path):
    """
    Estrae da una Form FoxPro (.scx) sia il CODICE (metodi) sia il LAYOUT
    GRAFICO (proprietà). Cruciale per permettere all'IA di ricreare la UX
    in tecnologie moderne.
    """
    try:
        table = DBF(file_path, ignore_missing_memofile=True, char_decode_errors='ignore')
        codice_form_estratto = []

        for record in table:
            # I campi Memo possono arrivare come None o bytes: normalizziamo
            # sempre a stringa prima di chiamare .strip().
            metodi = str(record.get('METHODS') or record.get('methods') or "")
            proprieta = str(record.get('PROPERTIES') or record.get('properties') or "")

            nome_oggetto = record.get('OBJNAME') or record.get('objname') or "OggettoSconosciuto"
            classe_oggetto = record.get('CLASS') or record.get('class') or "ClasseSconosciuta"

            if metodi.strip() or proprieta.strip():
                codice_form_estratto.append("\n==========================================")
                codice_form_estratto.append(
                    f"*** OGGETTO: {nome_oggetto} | CLASSE: {classe_oggetto} ***"
                )

                if proprieta.strip():
                    codice_form_estratto.append("--- PROPRIETA' UI (Layout & Bindings) ---")
                    codice_form_estratto.append(proprieta.strip())

                if metodi.strip():
                    codice_form_estratto.append("--- METODI E CODICE SORGENTE ---")
                    codice_form_estratto.append(metodi.strip())

        if codice_form_estratto:
            return "\n".join(codice_form_estratto)
        return f"Form {os.path.basename(file_path)} analizzata, ma vuota."
    except Exception as e:
        return f"Errore durante il parsing nativo della Form FoxPro (.scx): {e}"


def extract_foxpro_dbf_schema(file_path):
    """
    Estrae SOLO lo schema (struttura dei campi) di una tabella .dbf senza
    caricare i dati. Previene il sovraccarico di token causato da tabelle
    con megabyte di record.
    """
    try:
        # load=False: dbfread legge solo l'header, NON i record.
        table = DBF(file_path, load=False, char_decode_errors='ignore')

        schema_info = [f"--- SCHEMA TABELLA FOXPRO LEGACY: {os.path.basename(file_path)} ---"]
        for field in table.fields:
            # field.type: tipo FoxPro (C=Character, N=Numeric, D=Date, M=Memo, ...)
            schema_info.append(
                f"  - Campo: {field.name} | Tipo FoxPro: {field.type} | Lunghezza: {field.length}"
            )

        schema_info.append("--- FINE SCHEMA ---\n")
        return "\n".join(schema_info)
    except Exception as e:
        return f"Errore durante l'estrazione dello schema DBF: {e}"


# =====================================================================
# Estrazione dipendenze via micro-agente
# =====================================================================

def extract_dependencies_from_file(file_name, file_content, llm, tracker=None):
    """
    Usa un agente 'micro' per leggere il contenuto (codice o schema)
    e restituire un JSON strutturato con le dipendenze.

    `tracker` (TokenUsageTracker) accumula anche i token consumati da
    questi micro-agenti: senza, il conteggio della Fase 1 sarebbe monco.
    """
    extractor_agent = Agent(
        role='Dependency Extractor',
        goal='Estrarre le dipendenze strutturali software o database e restituire un JSON valido.',
        backstory=(
            "Sei un analista sintattico specializzato in refactoring e reverse "
            "engineering, incluso codice legacy di qualsiasi tecnologia."
        ),
        llm=llm,
        verbose=False,
        allow_delegation=False,
    )

    extraction_task = Task(
        description=(
            f"Analizza l'entità denominata '{file_name}'. "
            f"Il suo contenuto o schema estratto è il seguente:\n\n{file_content}\n\n"
            "Identifica le dipendenze (altre tabelle chiamate, altre form invocate "
            "tramite DO FORM, o file esterni).\n"
            "Restituisci SOLO un oggetto JSON con questo esatto formato:\n"
            "{\n"
            '  "file": "nome_di_questo_file",\n'
            '  "depends_on": ["tabella_db_o_form_collegata_1", "entita_collegata_2"]\n'
            "}\n"
            "Se non rilevi dipendenze esplicite, restituisci una lista vuota []."
        ),
        expected_output="Una stringa JSON formattata correttamente.",
        agent=extractor_agent,
    )

    crew = Crew(
        agents=[extractor_agent],
        tasks=[extraction_task],
        memory=False,
    )
    result = crew.kickoff()

    if tracker is not None:
        tracker.aggiungi_crew(crew, result)

    # Compatibilità tra versioni CrewAI: result.raw può non esistere
    testo_grezzo = getattr(result, "raw", None) or str(result)
    testo_pulito = testo_grezzo.replace('```json', '').replace('```', '').strip()

    try:
        dati = json.loads(testo_pulito)
        # Guardie sul formato: l'LLM potrebbe rispondere con tipi imprevisti
        if not isinstance(dati, dict):
            raise ValueError("JSON non è un oggetto")
        dati.setdefault("file", file_name)
        if not isinstance(dati.get("depends_on"), list):
            dati["depends_on"] = []
        return dati
    except (json.JSONDecodeError, ValueError):
        return {"file": file_name, "depends_on": []}


# =====================================================================
# Costruzione del grafo
# =====================================================================

def _estrai_contenuto_file(file_path, estensione, session_id):
    """
    Restituisce il contenuto testuale del file usando la strategia adatta
    all'estensione (parser nativi per FoxPro, lettura diretta altrimenti).
    Ritorna None se il file va saltato.
    """
    file = os.path.basename(file_path)

    if estensione == '.scx':
        log_message(session_id, f"Estrazione metodi e layout dalla Form FoxPro: {file} ...")
        return extract_foxpro_scx_code(file_path)

    if estensione == '.dbf':
        log_message(
            session_id,
            f"Estrazione dello schema della Tabella FoxPro: {file} (salto i record dati)...",
        )
        return extract_foxpro_dbf_schema(file_path)

    # File di codice tradizionali (.prg FoxPro, .py, .php, ecc.)
    try:
        if os.path.getsize(file_path) > MAX_FILE_SIZE:
            log_message(session_id, f"Salto {file}: file di testo troppo grande (>250KB).")
            return None
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()
    except OSError as e:
        log_message(session_id, f"Impossibile leggere il file {file}: {e}")
        return None

def raccogli_sorgenti(cartella_sorgente, max_caratteri=None, file_ammessi=None):
    """
    Rilegge i sorgenti dalla cartella di sessione e li restituisce come
    testo unico, pronto da passare agli agenti come evidenza primaria.

    Usata dalla Fase 2 (e potenzialmente dal Quality Gate): senza questo,
    architetto e DBA progettano basandosi solo sui documenti della Fase 1,
    cioè su una descrizione di secondo livello del sistema.
    """
    if max_caratteri is None:
        max_caratteri = MAX_CARATTERI_SORGENTI

    parti, usati = [], 0
    for root, dirs, files in os.walk(cartella_sorgente):
        dirs[:] = [d for d in dirs if d not in ESCLUDI_CARTELLE]
        for file in sorted(files):
            estensione = os.path.splitext(file)[1].lower()
            if estensione not in ESTENSIONI_VALIDE:
                continue
            file_path = os.path.join(root, file)
            relativo = os.path.relpath(file_path, cartella_sorgente).replace("\\", "/")
            if file_ammessi is not None and relativo not in file_ammessi:
                continue

            contenuto = _estrai_contenuto_file(file_path, estensione, None)
            if not contenuto:
                continue

            blocco = f"\n----- FILE: {relativo} -----\n{contenuto}\n"
            if usati + len(blocco) > max_caratteri:
                parti.append("\n[...contesto troncato per limiti di dimensione...]")
                return "".join(parti)
            parti.append(blocco)
            usati += len(blocco)

    return "".join(parti)

def _componi_contesto(G, sorgenti, session_id=None):
    """
    Contesto per gli agenti della Fase 1: il grafo delle relazioni PIÙ il
    codice sorgente reale.

    Senza i sorgenti gli agenti documentano solo nomi di moduli: non possono
    citare funzioni né logica, e i nodi imprecisi del grafo (dipendenze
    dedotte dal micro-agente) diventano "fatti" nei documenti finali.
    """
    parti = [_genera_report_grafo(G)]
    parti.append("\n\n" + "=" * 70)
    parti.append("CODICE SORGENTE DEI FILE ANALIZZATI")
    parti.append("Questa è l'evidenza primaria: in caso di discrepanza con il")
    parti.append("grafo qui sopra, fa fede il codice.")
    parti.append("=" * 70 + "\n")

    usati = 0
    for nome, contenuto in sorgenti:
        blocco = f"\n----- FILE: {nome} -----\n{contenuto}\n"
        if usati + len(blocco) > MAX_CARATTERI_SORGENTI:
            parti.append(
                f"\n[...{len(sorgenti)} file totali: il contesto è stato troncato "
                "per limiti di dimensione. I file non inclusi restano nel grafo...]"
            )
            break
        parti.append(blocco)
        usati += len(blocco)

    if session_id:
        log_message(session_id, f"Contesto per gli agenti: grafo + {usati // 1000} KB di codice sorgente.")
    return "\n".join(parti)

def _genera_report_grafo(G):
    """Trasforma il grafo delle dipendenze in un report testuale ordinato per importanza."""
    gradi_ingresso = dict(G.in_degree())
    nodi_ordinati = sorted(gradi_ingresso.items(), key=lambda item: item[1], reverse=True)

    
    intestazione = "REPORT GRAFO DELLE DIPENDENZE E SCHEMI DATABASE"
    report_grafo = intestazione + ":\n\n"
    righe = ["REPORT GRAFO DELLE DIPENDENZE E SCHEMI DATABASE:\n"]
    for nodo, conteggio in nodi_ordinati:
        dipendenze = list(G.successors(nodo))
        righe.append(f"- Modulo/File: {nodo}")
        righe.append(f"  Importanza relazionale: rilevato in {conteggio} flussi software.")
        if dipendenze:
            righe.append(f"  Interagisce/Dipende da: {', '.join(dipendenze)}")
        righe.append("")

    return "\n".join(righe)


def process_directory_to_graph(cartella_sorgente, llm, session_id, tracker=None, file_ammessi=None):
    """
    Itera sui file applicando filtri avanzati (parser nativi per FoxPro,
    lettura diretta per il codice standard), costruisce il grafo delle
    dipendenze via IA e scrive i log in tempo reale per il frontend.
    """
    G = nx.DiGraph()
    sorgenti = []  

    for root, dirs, files in os.walk(cartella_sorgente):
        dirs[:] = [d for d in dirs if d not in ESCLUDI_CARTELLE]

        for file in files:
            file_path = os.path.join(root, file)
            estensione = os.path.splitext(file)[1].lower()

            relativo = os.path.relpath(file_path, cartella_sorgente).replace("\\", "/")
            if file_ammessi is not None and relativo not in file_ammessi:
                continue

            content = _estrai_contenuto_file(file_path, estensione, session_id)
            if content is None:
                continue
            sorgenti.append((relativo, content))

            try:
                log_message(session_id, f"Analisi dipendenze IA per: {file} ...")
                interruzione.verifica_stop(session_id)
                if DELAY_TRA_FILE_SEC:
                    time.sleep(DELAY_TRA_FILE_SEC)
                dati_json = extract_dependencies_from_file(file, content, llm, tracker=tracker)
                nodo_principale = dati_json.get("file", file)
                G.add_node(nodo_principale)

                for dipendenza in dati_json.get("depends_on", []):
                    G.add_edge(nodo_principale, dipendenza)
            except interruzione.FaseInterrotta:
                raise
            except Exception as e:
                log_message(session_id, f"Errore IA su {file}: {e}")

    log_message(session_id, "Calcolo delle dipendenze strutturali completato. Generazione report...")
    return _componi_contesto(G, sorgenti, session_id)
