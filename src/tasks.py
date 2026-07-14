from crewai import Task

def get_understanding_tasks(agents, output_dir):
    """
    Ritorna i task per la FASE 1: Understanding (Universale).
    L'output combinato di questi task fermerà il flusso per il CHECK POINT 1.
    """
    
    assessment_task = Task(
        description=(
            "Analizza attentamente il seguente codice sorgente legacy e la documentazione del sistema forniti dal cliente:\n\n"
            "\"\"\"\n{codice_legacy}\n\"\"\"\n\n"
            "Esegui un'analisi statica approfondita. Identifica tutti i componenti del sistema sorgente. "
            "Mappa le strutture dati, i moduli software, le dipendenze esterne, le costanti e l'inventario "
            "complessivo dell'applicativo legacy analizzato."
        ),
        expected_output=(
            "Un documento di 'Inventory' strutturato in formato Markdown che elenca in modo esaustivo "
            "tutti gli asset identificati nel codice legacy, la tipologia dei file, le dimensioni e l'analisi statica iniziale."
        ),
        agent=agents["legacy_system_analyzer"],
        output_file=f"{output_dir}/1_Assessment_Inventory.md"
    )

    map_dependency_task = Task(
        description=(
            "Analizza l'inventario e il codice legacy per costruire una mappa dettagliata delle dipendenze interne ed esterne. "
            "Individua le relazioni e le interazioni reciproche tra: moduli applicativi, librerie, file di configurazione, "
            "schemi o tabelle del database e script di orchestrazione o processi batch."
        ),
        expected_output=(
            "Un report di 'Dependency Map' in formato Markdown che rappresenta (testualmente o tramite tabelle strutturate) "
            "il grafo delle dipendenze, evidenziando i punti critici di accoppiamento e i flussi dei dati."
        ),
        agent=agents["dependency_mapper"],
        output_file=f"{output_dir}/2_Map_Dependency.md"
    )

    # 1. Il Documentatore Tecnico ora fa SOLO la parte tecnica
    documentation_task = Task(
        description=(
            "Prendi in carico i dati dell'Assessment e della Mappa delle Dipendenze. "
            "Genera la documentazione puramente TECNICA del sistema legacy: "
            "struttura del codice originario, flussi logici, gestione I/O, pattern architetturali rilevati e come i dati persistono."
        ),
        expected_output=(
            "Un documento in Markdown contenente la Technical Documentation dettagliata del software originale."
        ),
        agent=agents["tech_business_documenter"],
        output_file=f"{output_dir}/3a_Technical_Documentation.md"
    )

    # 2. Il NUOVO Analista Funzionale crea il Backlog
    functional_analysis_task = Task(
        description=(
            "Analizza l'Assessment, la Mappa delle Dipendenze e il codice sorgente originario. "
            "Astrai la logica di programmazione per individuare i processi di business reali. "
            "Scrivi un Product Backlog in stile Agile contenente:\n"
            "1. Epics (le macro-funzionalità del sistema).\n"
            "2. User Stories scritte nel formato standard: 'Come [ruolo], voglio [azione] affinché [valore]'.\n"
            "3. Acceptance Criteria per ogni User Story (es. GIVEN, WHEN, THEN)."
        ),
        expected_output=(
            "Un documento Markdown strutturato come un Product Backlog Agile, pronto per Jira o Azure DevOps."
        ),
        agent=agents["functional_analyst"],
        output_file=f"{output_dir}/3b_Functional_Documentation.md"
    )

    
    test_book_task = Task(
        description=(
            "Sulla base della documentazione tecnica e funzionale appena prodotta, progetta una strategia di test per la migrazione. "
            "Genera automaticamente un set di Test Funzionali (scenari di test basati sulle regole di business estratte) "
            "e di Contract Test (test di contratto per garantire l'equivalenza delle interfacce e delle API di comunicazione)."
        ),
        expected_output=(
            "Un documento 'Test Book' strutturato in Markdown contenente le schede dei test funzionali e i vincoli "
            "dei Contract Test necessari a validare il successo della futura modernizzazione."
        ),
        agent=agents["qa_test_planner"],
        output_file=f"{output_dir}/4_Test_Book_Generation.md"
    )

    return [assessment_task, map_dependency_task, documentation_task, functional_analysis_task, test_book_task]


def get_design_tasks(agents, output_dir):
    """
    Ritorna i task per la FASE 2: Design (Universale).
    Prende in input i report validati della fase 1 e produce il piano architetturale.
    Si ferma per il CHECK POINT 2.
    """
    
    migration_plan_task = Task(
        description=(
            "Analizza la documentazione e il codice legacy validati nel Check Point 1 umano. "
            "Progetta la strategia di conversione e modernizzazione verso lo stack tecnologico target richiesto: {linguaggio_target}.\n"
            "Devi produrre:\n"
            "1. Un piano strutturato di migrazione (Migration Plan) con i passaggi logici di scomposizione.\n"
            "2. Gli Architectural Decision Records (ADR) che motivano formalmente la scelta dei nuovi pattern di design, "
            "la struttura delle cartelle, i modelli database e lo standard delle API nel nuovo sistema target."
        ),
        expected_output=(
            "Un documento in formato Markdown che include il Migration Plan completo e l'elenco formale "
            "degli ADR (Architectural Decision Records) per guidare lo sviluppo."
        ),
        agent=agents["cloud_solutions_architect"],
        output_file=f"{output_dir}/5_Migration_Plan_ADR.md"
    )


    dba_task = Task(
        description=(
            "Basandoti sui documenti di analisi della Fase 1 e sulle decisioni architetturali appena prese, progetta il nuovo database.\n"
            "Il target tecnologico richiesto dal cliente è: {linguaggio_target}.\n\n"
            "Esegui queste operazioni:\n"
            "1. Normalizza lo schema estratto dai vecchi file (es. DBF): individua relazioni implicite e definisci Primary/Foreign Key esplicite.\n"
            "2. Converti i vecchi tipi di dato FoxPro/Legacy nei tipi SQL moderni più adeguati.\n"
            "3. Produci uno script DDL completo con le istruzioni `CREATE TABLE`.\n"
            "4. Aggiungi i `CREATE INDEX` necessari per ottimizzare le query future."
        ),
        expected_output="Uno script SQL formattato correttamente contenente le istruzioni DDL per la creazione del nuovo database relazionale.",
        agent=agents["database_administrator"],
        output_file=f"{output_dir}/3_Database_Schema.sql" # Genera il file SQL!
    )


    
    return [migration_plan_task, dba_task]

# ... (lascia intatte le funzioni get_understanding_tasks e get_design_tasks sopra) ...

def get_iterative_implementation_tasks(agents, linguaggio_target, nome_file_legacy, contenuto_file_legacy, contesto_adr, contesto_sql):
    """
    Genera i task dinamicamente per UN SINGOLO file legacy, iniettando l'architettura globale.
    """
    
    backend_task = Task(
        description=f"""
        Sei un Senior Backend Developer. Il tuo obiettivo NON È TRADURRE il codice riga per riga.
        Devi estrarre la logica di business pura dal file legacy fornito e progettarla da zero in {linguaggio_target}.
        
        🔴 REGOLE ARCHITETTURALI (DA RISPETTARE RIGOROSAMENTE):
        Ecco il documento ADR approvato dal Cloud Architect. Devi usare ESATTAMENTE i pattern descritti qui:
        {contesto_adr}
        
        🔵 SCHEMA DATABASE TARGET:
        Usa SOLO le entità e i nomi colonna presenti in questo schema. Ignora le vecchie strutture dati legacy:
        {contesto_sql}
        
        📄 FILE LEGACY DA ANALIZZARE ({nome_file_legacy}):
        {contenuto_file_legacy}
        
        Ignora completamente la UI, i bottoni o le finestre. Crea solo Endpoint REST (Controller) e Classi di Servizio.
        
        ⚠️ FORMATO DI OUTPUT OBBLIGATORIO:
        /// FILEPATH: src/backend/...
        ```
        [tuo codice pulito qui]
        ```
        """,
        expected_output=f"Il codice sorgente Backend rifattorizzato per il file {nome_file_legacy}.",
        agent=agents["senior_migration_developer"]
    )

    frontend_task = Task(
        description=f"""
        Sei un Senior Frontend Developer. Basandoti sul file legacy e sul codice Backend appena generato dal tuo collega, 
        devi disegnare l'interfaccia utente moderna usando {linguaggio_target}.
        
        🔴 REGOLE ARCHITETTURALI:
        {contesto_adr}
        
        📄 FILE LEGACY ORIGINALE (Per capire l'intento della UX):
        {contenuto_file_legacy}
        
        Non usare librerie vecchie. Chiama gli endpoint REST del backend. 
        
        ⚠️ FORMATO DI OUTPUT OBBLIGATORIO:
        /// FILEPATH: src/frontend/...
        ```
        [tuo codice UI qui]
        ```
        """,
        expected_output=f"Il codice sorgente Frontend moderno (UI) per sostituire {nome_file_legacy}.",
        agent=agents["frontend_developer"]
    )

    return [backend_task, frontend_task]


def get_quality_check_task(agents, output_dir):
    """
    Task indipendente per il controllo qualità. Gira una volta sola alla fine.
    """
    quality_check_task = Task(
        description=(
            "Analizza accuratamente tutto il codice sorgente appena generato e le relative suite di test.\n"
            "Esegui un audit di qualità e sicurezza simulando i severi vincoli di SonarQube e le linee guida OWASP.\n"
            "Verifica che la copertura dei test (Coverage) sia superiore all'80%, mappa i potenziali code smell, "
            "le vulnerabilità di sicurezza e rilascia l'approvazione finale per la produzione."
        ),
        expected_output=(
            "Un report di 'Quality Check' in Markdown che certifica la conformità OWASP, documenta la code coverage (>80%), "
            "evidenzia le metriche in stile SonarQube e archivia gli artefatti nel Capability Registry."
        ),
        agent=agents["security_quality_reviewer"],
        output_file=f"{output_dir}/7_Quality_Check_Report.md"
    )

    return [quality_check_task]