from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from crewai import Task

from src.config import (
    CONVENZIONI_FASE1,
    FILE_ASSESSMENT,
    FILE_DEPENDENCY_MAP,
    FILE_TECH_DOC,
    FILE_FUNCTIONAL_DOC,
    FILE_DB_SCHEMA,
    FILE_TEST_BOOK,
    FILE_MIGRATION_PLAN,
    FILE_QUALITY_REPORT,
    MERMAID_RULES,
)

# =====================================================================
# HELPER PER LA SICUREZZA DEI PROMPT
# =====================================================================
def _escape_braces(text: str) -> str:
    """
    Sostituisce { con {{ e } con }} nei testi esterni (codice legacy, report generati).
    Previene l'errore "Missing required template variable" di LangChain/CrewAI,
    che cerca erroneamente di interpretare le graffe del codice o dei JSON come variabili.
    """
    if not text:
        return ""
    return str(text).replace("{", "{{").replace("}", "}}")


def _nota_data():
    """
    Gli LLM non conoscono la data corrente e la inventano (di solito nel
    passato, vicino al proprio addestramento). Gliela forniamo esplicitamente.
    """
    oggi = datetime.now(ZoneInfo("Europe/Rome")).strftime("%d/%m/%Y")
    return (
        f"\n\nDATA ODIERNA: {oggi}. "
        "Se il documento richiede una data (ADR, revisioni, cronologie), usa "
        "ESCLUSIVAMENTE questa. Non inventare date, anni o periodi: se "
        "un'informazione temporale non è deducibile dal codice sorgente "
        "analizzato, ometti il riferimento invece di stimarlo."
    )
# =====================================================================
# FASE 1 - UNDERSTANDING
# =====================================================================

def get_understanding_tasks(agents, output_dir):
    """
    Ritorna i task per la FASE 1: Understanding (Universale).
    L'output combinato di questi task fermerà il flusso per il CHECK POINT 1.
    """

    assessment_task = Task(
        description=(
            "Analizza attentamente il seguente codice sorgente legacy e la "
            "documentazione del sistema forniti dal cliente:\n\n"
            "\"\"\"\n{codice_legacy}\n\"\"\"\n\n"
            "Esegui un'analisi statica approfondita. Identifica tutti i componenti "
            "del sistema sorgente. Mappa le strutture dati, i moduli software, le "
            "dipendenze esterne, le costanti e l'inventario complessivo "
            "dell'applicativo legacy analizzato."
             + CONVENZIONI_FASE1
        )+ _nota_data(),
        expected_output=(
            "Un documento di 'Inventory' strutturato in formato Markdown che elenca "
            "in modo esaustivo tutti gli asset identificati nel codice legacy, la "
            "tipologia dei file, le dimensioni e l'analisi statica iniziale. "
            "VINCOLO DI COMPLETEZZA: il documento deve essere COMPLETO e "
            "autoconclusivo. Non annunciare nell'indice sezioni che non "
            "svilupperai: meglio 6 sezioni complete che 16 dichiarate e "
            "troncate. Non inserire rinvii a sezioni che non esistono nel "
            "documento. Concludi sempre il testo prima di esaurire lo spazio."
        ),
        agent=agents["legacy_system_analyzer"],
        output_file=f"{output_dir}/{FILE_ASSESSMENT}",
    )

    map_dependency_task = Task(
        description=(
            "Analizza l'inventario e il codice legacy per costruire una mappa "
            "dettagliata delle dipendenze interne ed esterne. Individua le relazioni "
            "e le interazioni reciproche tra: moduli applicativi, librerie, file di "
            "configurazione, schemi o tabelle del database e script di orchestrazione "
            "o processi batch."
            + MERMAID_RULES + CONVENZIONI_FASE1
        )+ _nota_data(),
        expected_output=(
            "Un report di 'Dependency Map' in formato Markdown che DEVE "
            "obbligatoriamente contenere: 1) un diagramma Mermaid (graph TD) "
            "che visualizza il grafo delle dipendenze tra moduli, form, tabelle "
            "e processi; 2) una tabella riassuntiva delle dipendenze; "
            "3) l'evidenza dei punti critici di accoppiamento. "
            "Un report senza il diagramma Mermaid è considerato incompleto."
            "VINCOLO DI COMPLETEZZA: il documento deve essere COMPLETO e "
            "autoconclusivo. Non annunciare nell'indice sezioni che non "
            "svilupperai: meglio 6 sezioni complete che 16 dichiarate e "
            "troncate. Non inserire rinvii a sezioni che non esistono nel "
            "documento. Concludi sempre il testo prima di esaurire lo spazio."
        ),
        agent=agents["dependency_mapper"],
        context=[assessment_task],
        output_file=f"{output_dir}/{FILE_DEPENDENCY_MAP}",
    )

    documentation_task = Task(
        description=(
            "Prendi in carico i dati dell'Assessment e della Mappa delle Dipendenze. "
            "Genera la documentazione puramente TECNICA del sistema legacy: struttura "
            "del codice originario, flussi logici, gestione I/O, pattern architetturali "
            "rilevati e come i dati persistono."
            + MERMAID_RULES + CONVENZIONI_FASE1
        ),
        expected_output=(
            "Un documento in Markdown contenente la Technical Documentation "
            "dettagliata del software originale."
            "VINCOLO DI COMPLETEZZA: il documento deve essere COMPLETO e "
            "autoconclusivo. Non annunciare nell'indice sezioni che non "
            "svilupperai: meglio 6 sezioni complete che 16 dichiarate e "
            "troncate. Non inserire rinvii a sezioni che non esistono nel "
            "documento. Concludi sempre il testo prima di esaurire lo spazio."
        )+ _nota_data(),
        agent=agents["tech_business_documenter"],
        context=[assessment_task, map_dependency_task],
        output_file=f"{output_dir}/{FILE_TECH_DOC}",
    )

    functional_analysis_task = Task(
        description=(
            "Analizza l'Assessment, la Mappa delle Dipendenze e il codice sorgente "
            "originario. Astrai la logica di programmazione per individuare i "
            "processi di business reali. Scrivi un Product Backlog in stile Agile "
            "contenente:\n"
            "1. Epics (le macro-funzionalità del sistema).\n"
            "2. User Stories scritte nel formato standard: "
            "'Come [ruolo], voglio [azione] affinché [valore]'.\n"
            "3. Acceptance Criteria per ogni User Story (es. GIVEN, WHEN, THEN)."
            + MERMAID_RULES + CONVENZIONI_FASE1
        )+ _nota_data(),
        expected_output=(
            "Un documento intitolato 'Documentazione Funzionale' in formato markdown strutturato che contiene un "
            "Product Backlog Agile completo: personas, capability, Epics, User "
            "Stories con criteri di accettazione. Il titolo del documento è "
            "'Documentazione Funzionale': il Product Backlog è la sua forma, "
            "non il suo nomeUn documento."
            "VINCOLO DI COMPLETEZZA: il documento deve essere COMPLETO e "
            "autoconclusivo. Non annunciare nell'indice sezioni che non "
            "svilupperai: meglio 6 sezioni complete che 16 dichiarate e "
            "troncate. Non inserire rinvii a sezioni che non esistono nel "
            "documento. Concludi sempre il testo prima di esaurire lo spazio."
        ),
        agent=agents["functional_analyst"],
        context=[assessment_task, map_dependency_task],
        output_file=f"{output_dir}/{FILE_FUNCTIONAL_DOC}",
    )

    test_book_task = Task(
        description=(
            "Sulla base della documentazione tecnica e funzionale appena prodotta, "
            "progetta una strategia di test per la migrazione. Genera automaticamente "
            "un set di Test Funzionali (scenari di test basati sulle regole di "
            "business estratte) e di Contract Test (test di contratto per garantire "
            "l'equivalenza delle interfacce e delle API di comunicazione)."
             + CONVENZIONI_FASE1
        ),
        expected_output=(
            "Un documento 'Test Book' strutturato in Markdown contenente le schede "
            "dei test funzionali e i vincoli dei Contract Test necessari a validare "
            "il successo della futura modernizzazione."
            "VINCOLO DI COMPLETEZZA: il documento deve essere COMPLETO e "
            "autoconclusivo. Non annunciare nell'indice sezioni che non "
            "svilupperai: meglio 6 sezioni complete che 16 dichiarate e "
            "troncate. Non inserire rinvii a sezioni che non esistono nel "
            "documento. Concludi sempre il testo prima di esaurire lo spazio."
        )+ _nota_data(),
        agent=agents["qa_test_planner"],
        context=[documentation_task, functional_analysis_task],
        output_file=f"{output_dir}/{FILE_TEST_BOOK}",
    )


    return [
        assessment_task,
        map_dependency_task,
        documentation_task,
        functional_analysis_task,
        test_book_task,
    ]


# =====================================================================
# FASE 2 - DESIGN
# =====================================================================

def get_design_tasks(agents, output_dir, contesto_fase1=""):
    """
    Ritorna i task per la FASE 2: Design (Universale).
    Prende in input i report validati della fase 1 e produce il piano architetturale.
    Si ferma per il CHECK POINT 2.
    """
    
    # 🛡️ PROTEZIONE: Disinnesca le graffe generate in Fase 1
    safe_contesto = _escape_braces(contesto_fase1)

  # Il contenuto dei documenti arriva via inputs al kickoff, NON incollato
    # qui: i documenti citano codice legacy pieno di graffe ({num1}, ecc.)
    # che il template engine di CrewAI scambierebbe per placeholder.
    blocco_contesto = (
        "\n\nDOCUMENTAZIONE VALIDATA DELLA FASE DI UNDERSTANDING:\n{contesto_fase1}\n"
    )

    migration_plan_task = Task(
        description=(
            "Analizza la documentazione e il codice legacy validati nel Check Point 1 "
            "umano. Progetta la strategia di conversione e modernizzazione verso lo "
            "stack tecnologico target richiesto: {linguaggio_target}.\n"
            "Devi produrre:\n"
            "1. Un piano strutturato di migrazione (Migration Plan) con i passaggi "
            "logici di scomposizione.\n"
            "2. Gli Architectural Decision Records (ADR) che motivano formalmente la "
            "scelta dei nuovi pattern di design, la struttura delle cartelle, i "
            "modelli database e lo standard delle API nel nuovo sistema target."
            + MERMAID_RULES
            + blocco_contesto
        )+ _nota_data(),
        expected_output=(
            "Un documento in formato Markdown che include il Migration Plan completo "
            "e l'elenco formale degli ADR (Architectural Decision Records) per "
            "guidare lo sviluppo."
            "VINCOLO DI COMPLETEZZA: il documento deve essere COMPLETO e "
            "autoconclusivo. Non annunciare nell'indice sezioni che non "
            "svilupperai: meglio 6 sezioni complete che 16 dichiarate e "
            "troncate. Non inserire rinvii a sezioni che non esistono nel "
            "documento. Concludi sempre il testo prima di esaurire lo spazio."
        ),
        agent=agents["cloud_solutions_architect"],
        output_file=f"{output_dir}/{FILE_MIGRATION_PLAN}",
    )

    dba_task = Task(
        description=(
            "Basandoti sui documenti di analisi della Fase 1 e sulle decisioni "
            "architetturali appena prese, progetta il nuovo database.\n"
            "Il target tecnologico richiesto dal cliente è: {linguaggio_target}.\n\n"
            "Esegui queste operazioni:\n"
            "1. Normalizza lo schema estratto dai vecchi file (es. DBF): individua "
            "relazioni implicite e definisci Primary/Foreign Key esplicite.\n"
            "2. Converti i vecchi tipi di dato FoxPro/Legacy nei tipi SQL moderni "
            "più adeguati.\n"
            "3. Produci uno script DDL completo con le istruzioni `CREATE TABLE`.\n"
            "4. Aggiungi i `CREATE INDEX` necessari per ottimizzare le query future.\n\n"
            "IMPORTANTE: l'output deve essere SOLO SQL valido (con commenti `--` "
            "dove serve), senza testo Markdown attorno, perché verrà salvato come "
            "file .sql eseguibile."
            + blocco_contesto
        )+ _nota_data(),
        expected_output=(
            "Uno script SQL formattato correttamente contenente le istruzioni DDL "
            "per la creazione del nuovo database relazionale."
            "VINCOLO DI COMPLETEZZA: il documento deve essere COMPLETO e "
            "autoconclusivo. Non annunciare nell'indice sezioni che non "
            "svilupperai: meglio 6 sezioni complete che 16 dichiarate e "
            "troncate. Non inserire rinvii a sezioni che non esistono nel "
            "documento. Concludi sempre il testo prima di esaurire lo spazio."
        ),
        agent=agents["database_administrator"],
        context=[migration_plan_task],
        output_file=f"{output_dir}/{FILE_DB_SCHEMA}",
    )

    return [migration_plan_task, dba_task]


# =====================================================================
# FASE 3 - IMPLEMENTATION (iterativa, un file legacy alla volta)
# =====================================================================

@dataclass
class ImplementationTasks:
    """
    Contratto esplicito della coppia di task iterativi.
    """
    backend: Task
    frontend: Task

    def as_list(self):
        return [self.backend, self.frontend]


def get_iterative_implementation_tasks(
    agents,
    linguaggio_target,
    nome_file_legacy,
    contenuto_file_legacy,
    contesto_adr,
    contesto_sql,
    contesto_funzionale="",
    contesto_test="",
):
    """
    Genera i task dinamicamente per UN SINGOLO file legacy,
    iniettando l'architettura globale (ADR + schema DB).
    """

    # 🛡️ PROTEZIONE: Disinnesca le graffe dal codice legacy originale e dai report
    safe_legacy = _escape_braces(contenuto_file_legacy)
    safe_adr = _escape_braces(contesto_adr)
    safe_sql = _escape_braces(contesto_sql)
    safe_funzionale = _escape_braces(contesto_funzionale)
    safe_test = _escape_braces(contesto_test)

    backend_task = Task(
        description=f"""
        Sei un Senior Backend Developer. Il tuo obiettivo NON È TRADURRE il codice riga per riga.
        Devi estrarre la logica di business pura dal file legacy fornito e progettarla da zero in {linguaggio_target}.

        REGOLE ARCHITETTURALI (DA RISPETTARE RIGOROSAMENTE):
        Ecco il documento ADR approvato dal Cloud Architect. Devi usare ESATTAMENTE i pattern descritti qui:
        {safe_adr}

        SCHEMA DATABASE TARGET:
        Usa SOLO le entità e i nomi colonna presenti in questo schema. Ignora le vecchie strutture dati legacy:
        {safe_sql}

        REQUISITI FUNZIONALI DA IMPLEMENTARE (documentazione validata in Fase 1):
        Il codice che scrivi deve soddisfare queste User Story e i relativi criteri di accettazione.
        {safe_funzionale}

        TEST CHE IL CODICE DOVRÀ SUPERARE:
        Scrivi gli unit test coerenti con questi casi di test già definiti.
        {safe_test}
        
        FILE LEGACY DA ANALIZZARE ({nome_file_legacy}):
        {safe_legacy}

        Ignora completamente la UI, i bottoni o le finestre. Crea solo Endpoint REST (Controller) e Classi di Servizio.

        FORMATO DI OUTPUT OBBLIGATORIO (ripetibile per ogni file generato):
        /// FILEPATH: src/backend/...
        ```
        [tuo codice pulito qui]
        ```
        Il percorso dopo /// FILEPATH: deve essere sempre RELATIVO (mai assoluto, mai contenente '..').
        """,
        expected_output=(
            f"Il codice sorgente Backend rifattorizzato per il file {nome_file_legacy}, "
            "completo di unit test, nel formato /// FILEPATH richiesto."
        ),
        agent=agents["senior_migration_developer"],
    )

    frontend_task = Task(
        description=f"""
        Sei un Senior Frontend Developer. Basandoti sul file legacy e sul codice Backend appena generato dal tuo collega,
        devi disegnare l'interfaccia utente moderna usando {linguaggio_target}.

        REGOLE ARCHITETTURALI:
        {safe_adr}

        REQUISITI FUNZIONALI E UX ATTESA:
        {safe_funzionale}

        FILE LEGACY ORIGINALE (per capire l'intento della UX):
        {safe_legacy}

        Non usare librerie vecchie. Chiama gli endpoint REST del backend.

        FORMATO DI OUTPUT OBBLIGATORIO (ripetibile per ogni file generato):
        /// FILEPATH: src/frontend/...
        ```
        [tuo codice UI qui]
        ```
        Il percorso dopo /// FILEPATH: deve essere sempre RELATIVO (mai assoluto, mai contenente '..').
        """,
        expected_output=(
            f"Il codice sorgente Frontend moderno (UI) per sostituire {nome_file_legacy}, "
            "nel formato /// FILEPATH richiesto."
        ),
        agent=agents["frontend_developer"],
        context=[backend_task],
    )

    return ImplementationTasks(backend=backend_task, frontend=frontend_task)


# =====================================================================
# QUALITY CHECK FINALE
# =====================================================================

def get_quality_check_task(
    agents,
    output_dir,
    codice_da_analizzare="",
    chunk_label="",
    output_filename=FILE_QUALITY_REPORT,
):
    """
    Task per il controllo qualità.
    """
    
    # 🛡️ PROTEZIONE: Disinnesca le graffe dal codice sorgente appena generato
    safe_codice = _escape_braces(codice_da_analizzare)
    
    intestazione_chunk = f" ({chunk_label})" if chunk_label else ""

    quality_check_task = Task(
        description=(
            f"Analizza accuratamente il seguente codice sorgente generato{intestazione_chunk} "
            "e le relative suite di test:\n\n"
            f"\"\"\"\n{safe_codice}\n\"\"\"\n\n"
            "Esegui una revisione di sicurezza e qualità basata sulla LETTURA del codice, "
            "secondo le linee guida OWASP Top 10.\n"
            "Individua: vulnerabilità (SQL Injection, XSS, credenziali cablate, gestione "
            "errata degli errori), code smell, duplicazioni, complessità eccessiva e "
            "lacune evidenti nella copertura dei test (quali comportamenti restano "
            "senza test). Indica chiaramente se il codice è approvabile per la "
            "produzione o quali correzioni sono bloccanti.\n\n"
            "VINCOLO OBBLIGATORIO: non riportare MAI metriche numeriche che "
            "richiederebbero l'esecuzione di strumenti (percentuali di code coverage, "
            "complessità ciclomatica misurata, punteggi o rating SonarQube). "
            "Sono dati che non puoi misurare leggendo il codice: riportarli sarebbe "
            "un'informazione falsa. Limitati a rilievi qualitativi verificabili, "
            "citando file, funzione o riga."
        )+ _nota_data(),
        expected_output=(
            "Un report di 'Quality Check' in Markdown con: sintesi della revisione, "
            "tabella dei rilievi (tipo | descrizione | posizione nel codice | gravità "
            "alta/media/bassa), lacune di test individuate e verdetto finale "
            "(APPROVATO / APPROVATO CON RISERVE / RESPINTO) con le motivazioni. "
            "Nessuna percentuale di copertura né punteggio numerico."
        ),
        agent=agents["security_quality_reviewer"],
        output_file=f"{output_dir}/{output_filename}",
    )

    return [quality_check_task]

# =====================================================================
# validatore dei task
# =====================================================================

def get_validation_task(agent, output_dir, nome_fase, output_filename):
    """
    Task di validazione documentale (Quality Gate).
    NOTA: il contenuto dei documenti NON va concatenato qui — arriva via
    inputs del kickoff come {contenuto_fase}, altrimenti le graffe presenti
    nel codice legacy citato verrebbero scambiate per template variable.
    """
    return [Task(
        description=(
            f"Sei il Quality Gate della fase '{nome_fase}'. Di seguito trovi i "
            "documenti prodotti dagli agenti in questa fase:\n\n"
            "{contenuto_fase}\n\n"
            "Valuta questi documenti secondo i criteri seguenti:\n"
            "1. COMPLETEZZA: ogni documento copre ciò che dichiara di coprire? "
            "Ci sono sezioni vuote, troncate o dichiarate e non sviluppate?\n"
            "2. COERENZA INTERNA: i documenti si contraddicono tra loro? "
            "I riferimenti incrociati (nomi di file, tabelle, moduli) coincidono?\n"
            "3. AFFIDABILITÀ: ci sono affermazioni generiche o non supportate dal "
            "codice analizzato (allucinazioni, componenti mai citati nel sorgente)?\n"
            "4. UTILIZZABILITÀ: la fase successiva ha tutto ciò che le serve per "
            "lavorare senza dover indovinare?\n\n"
            "Scrivi in ITALIANO. Sii concreto: cita il documento e la sezione per "
            "ogni rilievo. Non riscrivere i documenti, non aggiungere analisi tue."
        )+ _nota_data(),
        expected_output=(
            "Un report di validazione in Markdown che inizia OBBLIGATORIAMENTE con "
            "una riga nel formato esatto:\n"
            "ESITO: PROMOSSO  (oppure)  ESITO: PROMOSSO CON RISERVA  (oppure)  ESITO: BOCCIATO\n\n"
            "Seguono: una sintesi di 3-4 righe, la tabella dei rilievi "
            "(documento | criticità | gravità alta/media/bassa) e le azioni "
            "consigliate. Massimo 800 parole: è un verdetto, non un trattato."
        ),
        agent=agent,
        output_file=f"{output_dir}/{output_filename}",
    )]