import json
import logging
import os
import time


import networkx as nx
from crewai import Agent, Task, Crew
from dbfread import DBF  # Libreria nativa per FoxPro
import dbfread.dbf as _dbfread_dbf
import dbfread.memo as _dbfread_memo


def _abilita_memo_foxpro():
    """
    Insegna a dbfread a leggere i file memo delle Form e Class Library FoxPro.

    dbfread cerca il memo solo come .fpt o .dbt, e lo apre col lettore FoxPro
    solo se l'estensione e' .fpt. Ma le Form (.scx) tengono metodi e proprieta'
    in un .sct, le Class Library (.vcx) in un .vct e i Menu (.mnx) in un
    .mnt: senza questa estensione la libreria non trovava il memo e — con
    ignore_missing_memofile=True — restituiva campi VUOTI in silenzio.

    Conseguenza misurata su un applicativo reale: 80 form su 129 file
    risultavano "analizzate, ma vuote", e gli agenti documentavano un sistema
    di cui non avevano mai visto meta' del codice.
    """
    ricerca_originale = _dbfread_memo.find_memofile
    apertura_originale = _dbfread_memo.open_memofile

    def trova_memo(nome_dbf):
        for estensione in ('.fpt', '.dbt', '.sct', '.vct', '.mnt',
                           '.dct', '.frt', '.lbt', '.pjt'):
            trovato = _dbfread_memo.ifind(nome_dbf, ext=estensione)
            if trovato:
                return trovato
        return None

    def apri_memo(nome_file, versione_db):
        # Tutti i memo FoxPro usano lo stesso formato del .fpt e vanno letti
        # dal lettore FoxPro, non da quello dBase IV (che fallisce con
        # "unpack requires a buffer of 8 bytes"):
        #   .sct Form | .vct Class Library | .mnt Menu
        #   .dct Database Container | .frt Report | .lbt Etichette
        #   .pjt Progetto
        if nome_file.lower().endswith(('.fpt', '.sct', '.vct', '.mnt',
                                       '.dct', '.frt', '.lbt', '.pjt')):
            return _dbfread_memo.VFPMemoFile(nome_file)
        return apertura_originale(nome_file, versione_db)

    # Il patch va applicato sul modulo dbf: e' li' che i nomi vengono risolti.
    _dbfread_dbf.find_memofile = trova_memo
    _dbfread_dbf.open_memofile = apri_memo
    _dbfread_memo.find_memofile = trova_memo
    _dbfread_memo.open_memofile = apri_memo


_abilita_memo_foxpro()

# log_message vive ora in src/live_log.py (stessa cartella di scrittura e
# lettura dei log live). L'import resta qui anche come re-export per il
# codice esistente che lo importava da questo modulo.
import interruzione
from src.config import DELAY_TRA_FILE_SEC, MAX_CARATTERI_SORGENTI, contesto_max_per_modello
from src.live_log import log_message

logger = logging.getLogger(__name__)

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
# Estensioni ammesse all'analisi.
#
# REGOLA: qui entra solo cio' che contiene CODICE o STRUTTURE leggibili.
# Un formato binario letto come testo non produce un errore: produce
# caratteri illeggibili che finiscono nel contesto degli agenti e li
# confondono, peggio del non leggerlo affatto. Chi ha un estrattore
# dedicato e' segnalato; tutto il resto deve essere testo semplice.
ESTENSIONI_VALIDE = {
    # --- Visual FoxPro ------------------------------------------------
    '.prg',      # programmi e classi: testo
    '.scx',      # Form: DBF binario -> estrattore dedicato (memo .sct)
    '.vcx',      # Class Library: DBF binario -> memo .vct
    '.mnx',      # Menu: DBF binario -> estrattore dedicato (memo .mnt)
    '.dbf',      # Tabelle: estrattore dedicato, solo schema (non i dati)
    '.spr',      # screen generato da FoxPro 2.x: testo
    '.qpr',      # query generate: testo
    '.h',        # header di costanti (#DEFINE): testo

    # --- Visual Basic 6 -----------------------------------------------
    '.bas',      # moduli standard
    '.cls',      # moduli di classe
    '.frm',      # form: TESTO (le risorse binarie stanno nel .frx, escluso)
    '.ctl',      # user control
    '.vbp',      # progetto: elenco file e riferimenti
    '.vbg',      # gruppo di progetti
    # .dsr e .dsx (designer) sono spesso binari: esclusi di proposito

    # --- Delphi / Pascal ----------------------------------------------
    '.pas',      # unit
    '.dpr',      # progetto
    '.dpk',      # package
    '.inc',      # include
    '.dof',      # opzioni di progetto: testo, utile per le dipendenze
    # ATTENZIONE: .dfm puo' essere salvato in formato TESTO o BINARIO a
    # seconda del progetto. Incluso perche' nella maggior parte dei casi
    # e' testo; se arriva binario il controllo a valle lo segnala.
    '.dfm',      # form Delphi (testo nella maggior parte dei progetti)

    # --- COBOL e mainframe --------------------------------------------
    '.cbl', '.cob',   # programmi
    '.cpy',           # copybook: e' qui che stanno le strutture dati
    '.jcl',           # job control
    '.pco',           # COBOL con SQL embedded
    '.ddl',           # definizioni di schema

    # --- AS/400 - RPG --------------------------------------------------
    '.rpg', '.rpgle', '.sqlrpgle',
    '.clp', '.clle',  # Control Language
    '.dds',           # descrizioni di file e video
    '.pf', '.lf',     # physical/logical file (quando esportati come sorgente)

    # --- Altri legacy ---------------------------------------------------
    '.f', '.f77', '.f90', '.for',           # Fortran
    '.asm', '.s',                            # Assembly
    '.pl', '.pm',                            # Perl
    '.tcl', '.awk',                          # scripting legacy
    '.4gl', '.per',                          # Informix 4GL
    '.p', '.w', '.i',                        # Progress OpenEdge
    '.abap',                                 # SAP ABAP
    '.vb',                                   # VB.NET
    '.pb', '.sr', '.srw',                    # PowerBuilder (export testuale)

    # --- Linguaggi moderni (sistemi misti e stack target) -------------
    '.cs', '.java', '.py', '.js', '.ts', '.jsx', '.tsx',
    '.php', '.go', '.rs', '.rb', '.kt', '.swift', '.scala',
    '.c', '.cpp', '.cc', '.cxx', '.hpp',
    '.razor', '.vue',

    # --- Dati, configurazione, markup ---------------------------------
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
        # Una form senza metodi NE' proprieta' e' quasi sempre un problema di
        # lettura, non una form davvero vuota: senza questo avviso il caso
        # passava inosservato e il documento finale diceva "codice non
        # fornito" mentre il codice c'era.
        logger.warning("Form %s letta senza metodi ne' proprieta': "
                       "manca il file .sct affiancato?", os.path.basename(file_path))
        return f"Form {os.path.basename(file_path)} analizzata, ma vuota."
    except Exception as e:
        return f"Errore durante il parsing nativo della Form FoxPro (.scx): {e}"


def extract_foxpro_mnx_code(file_path):
    """
    Estrae da un Menu FoxPro (.mnx) la struttura delle voci e il CODICE
    associato a ciascuna.

    In un gestionale FoxPro il menu non e' decorazione: e' la mappa delle
    funzionalita' esposte all'utente, e ogni voce contiene la chiamata che
    apre una form o lancia una procedura. Senza, gli agenti vedono i moduli
    ma non sanno come ci si arriva ne' quali siano i punti d'ingresso reali.

    Come per le Form, il codice sta nel file memo affiancato (.mnt).
    """
    try:
        table = DBF(file_path, ignore_missing_memofile=True, char_decode_errors='ignore')
        voci = []

        for record in table:
            # I nomi dei campi cambiano di poco fra versioni: si tenta in
            # maiuscolo e minuscolo come per gli altri estrattori.
            def campo(*nomi):
                for n in nomi:
                    v = record.get(n) or record.get(n.lower()) or record.get(n.upper())
                    if v:
                        return str(v).strip()
                return ""

            etichetta = campo("PROMPT")
            comando = campo("COMMAND")
            procedura = campo("PROCEDURE")
            nome = campo("NAME", "OBJNAME")

            if not (etichetta or comando or procedura):
                continue

            voci.append("\n==========================================")
            voci.append(f"*** VOCE DI MENU: {etichetta or nome or 'senza etichetta'} ***")
            if nome:
                voci.append(f"Nome interno: {nome}")
            if comando:
                # E' qui che si legge quale form o procedura viene aperta.
                voci.append("--- COMANDO ASSOCIATO ---")
                voci.append(comando)
            if procedura:
                voci.append("--- PROCEDURA ---")
                voci.append(procedura)

        if voci:
            return "\n".join(voci)
        logger.warning("Menu %s letto senza voci: manca il file .mnt affiancato?",
                       os.path.basename(file_path))
        return f"Menu {os.path.basename(file_path)} analizzato, ma vuoto."
    except Exception as e:
        return f"Errore durante il parsing nativo del Menu FoxPro (.mnx): {e}"


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


def tenta_lettura_come_dbf(file_path):
    """
    Ultimo tentativo su un binario di formato SCONOSCIUTO: provare ad aprirlo
    come tabella DBF.

    Nel mondo legacy moltissimi formati sono DBF travestiti — in FoxPro lo
    sono le Form (.scx), le Class Library (.vcx), i Menu (.mnx), i Report
    (.frx), le Etichette (.lbx), i Progetti (.pjx) e i Database Container
    (.dbc); fuori da FoxPro lo sono dBase, Clipper e diversi gestionali
    proprietari.

    Invece di elencare ogni estensione — che sarebbe un elenco sempre
    incompleto — si TENTA. Se il file ha un header DBF valido si estrae quello
    che c'e' dentro; altrimenti si restituisce None e il file resta escluso
    con il suo motivo, come prima.

    E' il modo per essere generalisti senza scrivere un parser per ogni
    formato: costa un tentativo fallito sui binari veri, e recupera contenuto
    reale su tutti i DBF che non conosciamo.
    """
    try:
        table = DBF(file_path, ignore_missing_memofile=True, char_decode_errors="ignore")
        campi = [f.name for f in table.fields]
        if not campi:
            return None
    except Exception:
        return None          # non e' un DBF: nessun rumore nei log

    nome = os.path.basename(file_path)
    parti = [f"--- STRUTTURA DBF RICONOSCIUTA: {nome} ---",
             f"Campi: {', '.join(campi)}"]

    # I campi memo di questi formati contengono il codice: se ci sono, si
    # estraggono come si fa per le Form.
    campi_memo = [f.name for f in table.fields if f.type in ("M", "G")]
    if campi_memo:
        estratti = 0
        try:
            for record in table:
                for campo in campi_memo:
                    contenuto = str(record.get(campo) or "").strip()
                    if len(contenuto) > 20:
                        etichetta = str(record.get("OBJNAME") or record.get("NAME")
                                        or record.get("PROMPT") or f"record {estratti + 1}")
                        parti.append(f"\n*** {etichetta} | campo {campo} ***")
                        parti.append(contenuto)
                        estratti += 1
        except Exception as e:
            parti.append(f"[lettura dei memo interrotta: {type(e).__name__}]")
        if estratti:
            logger.info("Formato sconosciuto %s letto come DBF: %d blocchi di contenuto.",
                        nome, estratti)

    parti.append("--- FINE ---\n")
    return "\n".join(parti)


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
            "Identifica le dipendenze DISTINGUENDOLE PER TIPO. Sono relazioni di "
            "natura diversa e vanno tenute separate: mescolarle rende impossibile "
            "capire quali moduli formano un dominio e quali dati sono condivisi "
            "fra domini.\n"
            "- `chiama`: altri MODULI DI CODICE invocati (DO, DO FORM, CALL, "
            "SET PROCEDURE, import di classi). E' la dipendenza funzionale: dice "
            "quali file lavorano insieme.\n"
            "- `usa_dati`: TABELLE, viste o cursori letti o scritti (USE, SELECT, "
            "INSERT, UPDATE, nomi di tabella nelle query). Dice quali strutture "
            "dati il modulo tocca.\n"
            "- `risorse`: file statici non eseguibili (immagini .bmp/.ico, "
            "documenti, fogli di calcolo). Servono all'inventario ma NON sono "
            "dipendenze architetturali.\n"
            "Restituisci SOLO un oggetto JSON con questo esatto formato:\n"
            "{\n"
            '  "file": "nome_di_questo_file",\n'
            '  "chiama": ["modulo1.prg", "form2.scx"],\n'
            '  "usa_dati": ["tabella1", "vista2"],\n'
            '  "risorse": ["icona.bmp"]\n'
            "}\n"
            "Ogni lista puo' essere vuota. Non inserire lo stesso nome in due "
            "liste diverse: scegli quella che descrive come il file lo usa "
            "DAVVERO."
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

def _sembra_binario(testo, soglia=0.15):
    """
    True se il testo letto e' con ogni probabilita' un file binario.

    Serve perche' alcune estensioni ammesse esistono in due varianti (il .dfm
    Delphi puo' essere testo o binario) e perche' un formato inatteso non deve
    finire nel contesto come sequenza di caratteri illeggibili: un agente che
    riceve spazzatura produce documentazione basata su spazzatura, senza che
    nessuno se ne accorga.

    Criterio: presenza di byte nulli, oppure una quota eccessiva di caratteri
    di controllo sul campione iniziale.
    """
    if not testo:
        return False
    campione = testo[:4000]
    if "\x00" in campione:
        return True
    controllo = sum(1 for c in campione
                    if ord(c) < 32 and c not in "\t\n\r")
    return (controllo / len(campione)) > soglia


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

    if estensione == '.mnx':
        log_message(session_id, f"Estrazione voci e comandi dal Menu FoxPro: {file} ...")
        return extract_foxpro_mnx_code(file_path)

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
            contenuto = f.read()
        if _sembra_binario(contenuto):
            # Prima di arrendersi: moltissimi formati legacy sono DBF
            # travestiti (report, etichette, progetti, database container).
            # Se lo e', se ne estrae il contenuto invece di perderlo.
            recuperato = tenta_lettura_come_dbf(file_path)
            if recuperato:
                log_message(session_id, f"{file}: formato binario riconosciuto come tabella DBF, contenuto estratto.")
                return recuperato
            # Alcuni formati esistono in due varianti: un .dfm Delphi puo'
            # essere testo o binario a seconda di come e' stato salvato.
            # Passarlo agli agenti come testo significherebbe riempire il
            # contesto di caratteri illeggibili: meglio dirlo e saltarlo.
            log_message(session_id, f"Salto {file}: sembra un file binario, non testo leggibile.")
            logger.warning("File %s scartato: contenuto binario in un'estensione attesa come testo.", file)
            return None
        return contenuto
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

# Estensioni e marcatori dei componenti che NON fanno parte del sistema:
# driver, librerie di sistema, automazione Office.
_MARCATORI_ESTERNI = (
    ".dll", ".ocx", ".tlb", "zkemkeeper", "chilkat", "msxml", "scripting.",
    "excel.", "word.", "adodb.", "wtsapi", "advapi", "iphlpapi", "kernel32",
    "user32", "shell.", "wscript",
)

# Oltre questa quota di moduli che lo chiamano, un file e' una libreria di
# utilita': non definisce un dominio, lo attraversa.
_SOGLIA_UTILITA = 0.10


def _componenti_esterni(G):
    """Nodi che sono componenti di terze parti, non file del sistema."""
    return {
        n for n in G.nodes()
        if any(m in str(n).lower() for m in _MARCATORI_ESTERNI)
    }


def _librerie_di_utilita(G, soglia=_SOGLIA_UTILITA):
    """
    Moduli chiamati da una quota rilevante del sistema.

    Sono le librerie di funzioni condivise (in FoxPro tipicamente un file di
    UDF impostato con SET PROCEDURE): legano fra loro moduli che non hanno
    nulla in comune, e tenerli nel grafo dei domini produce un unico blocco.
    """
    chiamanti = {}
    for a, b, dati in G.edges(data=True):
        if dati.get("tipo") in ("chiama", "non_classificata"):
            chiamanti.setdefault(b, set()).add(a)
    moduli = {a for a, _, d in G.edges(data=True)
              if d.get("tipo") in ("chiama", "non_classificata")}
    if not moduli:
        return set()
    minimo = max(3, int(len(moduli) * soglia))
    return {b for b, chi in chiamanti.items() if len(chi) >= minimo}


def analizza_cluster(G):
    """
    Individua i DOMINI FUNZIONALI usando le sole dipendenze di CHIAMATA.

    Il punto decisivo e' l'esclusione degli archi verso le tabelle. In un
    gestionale le stesse strutture dati sono lette da moduli di aree diverse:
    contandole come dipendenze, ogni dominio risulta legato a tutti gli altri
    e il clustering restituisce un unico grumo — su un progetto reale il
    cluster piu' grande aveva 29 file e 151 legami esterni, cioe' piu' legami
    che file.

    Le tabelle condivise restano informazione preziosa, ma appartengono a
    un'altra domanda (dove si puo' tagliare il database) e si analizzano a
    parte con `analizza_dati_condivisi`.
    """
    if G is None or G.number_of_nodes() == 0:
        return []

    # Sottografo delle sole chiamate fra moduli, ESCLUSI due casi che
    # creerebbero domini fasulli:
    #
    # 1. i componenti ESTERNI (driver COM, DLL di sistema, librerie di terze
    #    parti): tre programmi che parlano con lo stesso lettore di badge non
    #    sono un dominio funzionale, sono solo file che usano lo stesso
    #    hardware. E' successo con `zkemkeeper.zkem.6`, che legava fra loro
    #    programmi senza alcuna relazione;
    # 2. le LIBRERIE DI UTILITA' chiamate da mezzo sistema: legano tutto con
    #    tutto e fanno collassare i domini, esattamente come facevano le
    #    tabelle condivise prima della tipizzazione.
    esterni = _componenti_esterni(G)
    utilita = _librerie_di_utilita(G)
    da_escludere = esterni | utilita

    chiamate = nx.DiGraph()
    for a, b, dati in G.edges(data=True):
        if dati.get("tipo") not in ("chiama", "non_classificata"):
            continue
        if b in da_escludere:
            continue
        chiamate.add_edge(a, b)
    if chiamate.number_of_nodes() == 0:
        chiamate = G.copy()          # grafo vecchio senza tipi: si usa tutto

    non_orientato = chiamate.to_undirected()
    try:
        # Trova i domini anche quando il grafo e' un unico blocco connesso,
        # cosa tipica dei monoliti: c'e' sempre un modulo di utilita' che
        # chiamano tutti.
        from networkx.algorithms.community import greedy_modularity_communities
        gruppi = [set(c) for c in greedy_modularity_communities(non_orientato)]
    except Exception:
        gruppi = [set(c) for c in nx.connected_components(non_orientato)]

    cluster = []
    for gruppo in gruppi:
        esterni = sum(1 for n in gruppo for v in non_orientato.neighbors(n)
                      if v not in gruppo)
        # Tabelle toccate dal dominio: servono a capire se e' migrabile da solo.
        tabelle = sorted({b for n in gruppo for _, b, d in G.out_edges(n, data=True)
                          if d.get("tipo") == "usa_dati"})
        cluster.append({
            "file": sorted(gruppo),
            "dimensione": len(gruppo),
            "legami_esterni": esterni,
            "tabelle_usate": tabelle,
        })
    cluster.sort(key=lambda c: -c["dimensione"])
    return cluster


def analizza_dati_condivisi(G, cluster):
    """
    Tabelle usate da PIU' domini: sono i punti in cui il database non si puo'
    tagliare senza decidere chi ne resta proprietario.

    E' la domanda che un architetto si pone per prima davanti a un monolite,
    e finora il DBA doveva dedurla leggendo tutto il codice.
    """
    if not cluster:
        return []

    dominio_di = {f: i for i, c in enumerate(cluster) for f in c["file"]}
    usata_da = {}
    for a, b, dati in G.edges(data=True):
        if dati.get("tipo") != "usa_dati":
            continue
        d = dominio_di.get(a)
        if d is not None:
            usata_da.setdefault(b, set()).add(d)

    condivise = [{"tabella": t, "domini": sorted(d), "quanti_domini": len(d)}
                 for t, d in usata_da.items() if len(d) > 1]
    condivise.sort(key=lambda x: -x["quanti_domini"])
    return condivise


def salva_grafo(G, output_dir, session_id=None):
    """
    Persiste il grafo delle dipendenze e i cluster individuati.

    Il grafo veniva costruito, usato una volta per comporre il contesto e poi
    buttato. E' invece l'unico dato OGGETTIVO sulla struttura del sistema —
    tutto il resto sono letture di un modello — e serve alle fasi successive.
    """
    if G is None:
        return None
    try:
        cluster = analizza_cluster(G)
        condivise = analizza_dati_condivisi(G, cluster)
        per_tipo = {}
        for _, _, d in G.edges(data=True):
            t = d.get("tipo", "non_classificata")
            per_tipo[t] = per_tipo.get(t, 0) + 1
        dati = {
            "nodi": G.number_of_nodes(),
            "archi": G.number_of_edges(),
            "archi_per_tipo": per_tipo,
            "dipendenze": {
                n: {
                    "chiama": sorted(b for _, b, d in G.out_edges(n, data=True)
                                     if d.get("tipo") in ("chiama", "non_classificata")),
                    "usa_dati": sorted(b for _, b, d in G.out_edges(n, data=True)
                                       if d.get("tipo") == "usa_dati"),
                }
                for n in G.nodes()
            },
            "cluster": cluster,
            "tabelle_condivise": condivise,
        }
        percorso = os.path.join(str(output_dir), "_grafo_dipendenze.json")
        with open(percorso, "w", encoding="utf-8") as f:
            json.dump(dati, f, ensure_ascii=False, indent=2)

        if session_id and cluster:
            isolati = sum(1 for c in cluster if c["legami_esterni"] == 0)
            log_message(
                session_id,
                f"🔗 Grafo: {G.number_of_nodes()} nodi, {G.number_of_edges()} dipendenze "
                f"({per_tipo.get('chiama', 0)} chiamate, {per_tipo.get('usa_dati', 0)} accessi a dati). "
                f"{len(cluster)} domini funzionali"
                + (f", {isolati} isolabili senza dipendenze esterne" if isolati else "")
                + (f". {len(condivise)} tabelle condivise fra domini" if condivise else "")
                + ".",
            )
        return dati
    except Exception as e:
        logger.warning("Grafo delle dipendenze non salvato: %s", e)
        return None


def _componi_contesto(G, sorgenti, session_id=None, modello=None):
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

    # Il tetto dipende dal modello scelto: su una finestra ampia troncare a
    # 600k sacrificherebbe qualita' che il cliente ha gia' pagato.
    massimo = contesto_max_per_modello(modello)

    usati, inclusi = 0, 0
    for nome, contenuto in sorgenti:
        blocco = f"\n----- FILE: {nome} -----\n{contenuto}\n"
        if usati + len(blocco) > massimo:
            parti.append(
                f"\n[...ATTENZIONE: di {len(sorgenti)} file ne sono inclusi {inclusi}. "
                "Il contesto e' stato troncato per limiti di dimensione: i file non "
                "inclusi compaiono nel grafo ma il loro codice NON e' disponibile. "
                "Dichiaralo esplicitamente nel documento invece di descriverli come "
                "se li avessi letti...]"
            )
            break
        parti.append(blocco)
        usati += len(blocco)
        inclusi += 1

    if session_id:
        if inclusi < len(sorgenti):
            # Va detto FORTE: un troncamento silenzioso fa credere che
            # l'analisi copra tutto il sistema quando ne copre una parte.
            log_message(
                session_id,
                f"⚠️ Contesto troncato: solo {inclusi} file su {len(sorgenti)} "
                f"({usati // 1000} KB su un tetto di {massimo // 1000} KB). "
                "I documenti copriranno solo questa porzione: valuta un modello "
                "con finestra piu' ampia o una selezione file piu' stretta.",
            )
            logger.warning("Contesto troncato per %s: %d file su %d (%d KB / %d KB).",
                           session_id, inclusi, len(sorgenti), usati // 1000, massimo // 1000)
        else:
            log_message(
                session_id,
                f"Contesto per gli agenti: grafo + {usati // 1000} KB di codice "
                f"sorgente ({inclusi} file, tutti inclusi).",
            )
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


def process_directory_to_graph(cartella_sorgente, llm, session_id, tracker=None,
                               file_ammessi=None, output_dir=None):
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

                # Gli archi sono TIPIZZATI: `chiama` e `usa_dati` sono
                # dipendenze architetturali di natura diversa, e tenerle
                # separate permette di analizzare i domini funzionali senza
                # che le tabelle condivise li facciano collassare in un unico
                # blocco. `risorse` resta fuori dal grafo: un'icona non e' una
                # dipendenza, e' un file allegato.
                for dipendenza in dati_json.get("chiama", []):
                    G.add_edge(nodo_principale, dipendenza, tipo="chiama")
                for tabella in dati_json.get("usa_dati", []):
                    G.add_edge(nodo_principale, tabella, tipo="usa_dati")

                # Compatibilita' con il formato precedente: se il modello
                # risponde ancora con `depends_on` non si perde nulla.
                for dipendenza in dati_json.get("depends_on", []):
                    if not G.has_edge(nodo_principale, dipendenza):
                        G.add_edge(nodo_principale, dipendenza, tipo="non_classificata")
            except interruzione.FaseInterrotta:
                raise
            except Exception as e:
                log_message(session_id, f"Errore IA su {file}: {e}")

    log_message(session_id, "Calcolo delle dipendenze strutturali completato. Generazione report...")

    # Il grafo e' l'unico dato oggettivo sulla struttura del sistema: va
    # conservato, non consumato e buttato.
    if output_dir:
        salva_grafo(G, output_dir, session_id)
    # Il nome del modello si ricava dall'oggetto LLM gia' in mano: evita di
    # propagare un parametro in piu' lungo tutta la catena di chiamate.
    nome_modello = getattr(llm, "model", None) or getattr(llm, "model_name", None)
    return _componi_contesto(G, sorgenti, session_id, modello=nome_modello)
