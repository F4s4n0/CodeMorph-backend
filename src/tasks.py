from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from crewai import Task

from src.config import (
    CONVENZIONI_FASE1,
    MAX_PROGETTI_ATTESI,
    STRUTTURA_SOLUTION_RULES,
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

def get_understanding_tasks(agents, output_dir, numero_file=0):
    """
    Ritorna i task per la FASE 1: Understanding (Universale).
    L'output combinato di questi task fermerà il flusso per il CHECK POINT 1.

    `numero_file` serve a calibrare l'ampiezza dei documenti. Il vincolo
    precedente era assoluto ("massimo 8 sezioni") e valeva identico per 4 file
    e per 278: su un applicativo grande gli agenti comprimevano tutto nello
    stesso spazio, citando 44 file su 278 e lasciando fuori il resto.

    La sostituzione NON e' un tetto piu' alto — sarebbe lo stesso errore con
    numeri diversi — ma un criterio di COPERTURA: ogni area funzionale una
    sezione, ogni file un posto. La lunghezza la detta il sistema analizzato.
    """
    # Nessun tetto numerico: sarebbe arbitrario (a 149 file un documento e'
    # "medio", a 150 "grande"?) e produrrebbe lo stesso difetto di prima, solo
    # con soglie diverse. Il criterio e' la COPERTURA: ogni area funzionale
    # deve avere la sua trattazione, ogni file un posto. Cosi' l'ampiezza
    # nasce dal sistema analizzato invece che da un numero deciso a priori.
    vincolo_ampiezza = (
        "AMPIEZZA DEL DOCUMENTO — si misura sulla COPERTURA, non sul numero di "
        "pagine: struttura il documento in tante sezioni quante sono le aree "
        "funzionali che hai effettivamente individuato nel codice, e assicurati "
        "che OGNI file analizzato trovi posto in almeno una di esse. Un modulo "
        "non citato e' un modulo che il cliente credera' non analizzato. "
        "Su un sistema piccolo verranno poche sezioni molto approfondite; su un "
        "sistema con centinaia di file ne serviranno molte di piu': in entrambi "
        "i casi e' il sistema a dettare la lunghezza, non un limite prefissato. "
        "NON diluire per allungare: se un'area e' semplice, poche righe "
        "specifiche valgono piu' di un paragrafo di prassi generiche. "
        "COMPLETEZZA: il documento deve essere autoconclusivo. Non annunciare "
        "nell'indice sezioni che non svilupperai, non inserire rinvii a sezioni "
        "inesistenti, e concludi il testo prima di esaurire lo spazio "
        "disponibile: meglio meno sezioni tutte complete che molte dichiarate "
        "e troncate."
    )
    if numero_file:
        # Il numero dei file e' l'unico dato oggettivo che l'agente non puo'
        # ricavare dal contesto (che riceve gia' concatenato): darglielo gli
        # permette di calibrare da solo quanto deve essere esteso il lavoro.
        vincolo_ampiezza = (
            f"Il codice legacy fornito comprende {numero_file} file distinti: "
            "il documento deve rendere conto di tutti. " + vincolo_ampiezza
        )

    assessment_task = Task(
        description=(
            "Analizza attentamente il seguente codice sorgente legacy e la "
            "documentazione del sistema forniti dal cliente:\n\n"
            "\"\"\"\n{codice_legacy}\n\"\"\"\n\n"
            "Esegui un'analisi statica approfondita. Identifica tutti i componenti "
            "del sistema sorgente. Mappa le strutture dati, i moduli software, le "
            "dipendenze esterne, le costanti e l'inventario complessivo "
            "dell'applicativo legacy analizzato.\n\n"
            "APRI il documento con un PROSPETTO QUANTITATIVO: una tabella che "
            "riporti, per ogni tipologia di file, QUANTI ne hai analizzati e a "
            "cosa servono. Esempio di formato:\n"
            "| Tipologia | N. | Ruolo nel sistema |\n"
            "|---|---|---|\n"
            "| Programmi (.prg) | 47 | Logica di business e procedure batch |\n"
            "| Form (.scx) | 80 | Interfaccia utente e logica negli eventi |\n"
            "| Tabelle (.dbf) | 31 | Persistenza dati |\n\n"
            "I numeri devono essere QUELLI REALI dei file che hai ricevuto, non "
            "una stima: il cliente li confronta con quanto ha caricato, ed e' cosi' "
            "che capisce se l'analisi ha coperto tutto.\n\n"
            "Poi, per OGNI tipologia, una TABELLA con una riga per file: nome, "
            "attributi rilevanti (dimensione, tabelle usate, dipendenze) e una "
            "descrizione di una riga di cosa fa. Le tabelle sono obbligatorie: "
            "un elenco puntato discorsivo non permette al cliente di verificare "
            "che il SUO file sia stato analizzato. Se una tipologia ha decine di "
            "file, raggruppa per area funzionale ma NON omettere righe.\n\n"
            "MA LE TABELLE NON SONO IL DOCUMENTO: sono il suo apparato. Ogni "
            "sezione dell'inventario si APRE con l'analisi in prosa di quel "
            "gruppo di file — a cosa serve nell'applicativo, come i file "
            "dialogano fra loro, quali dipendenze e quali rischi tecnici "
            "presenta — e la tabella viene DOPO, come dettaglio verificabile. "
            "Chiudi il documento con le criticita' trasversali emerse "
            "dall'analisi statica: duplicazioni, componenti obsoleti, "
            "accoppiamenti pericolosi, punti in cui la logica e' concentrata.\n"
            "Se il documento risultasse fatto quasi solo di tabelle, hai "
            "prodotto un elenco, non un assessment: il cliente non paga per "
            "sapere quali file possiede, ma per sapere COSA fanno e cosa "
            "comportano.\n\n"
            "CONTROLLO FINALE PRIMA DI CONSEGNARE: riprendi l'elenco dei file "
            "che hai ricevuto e verifica UNO A UNO che ciascuno compaia nel "
            "documento. Il conteggio del prospetto quantitativo deve "
            "corrispondere ai file effettivamente elencati nelle tabelle. "
            "Se ne trovi di non citati, aggiungili NELLA SEZIONE TEMATICA "
            "che gli compete, con la sua riga di tabella e la sua descrizione: "
            "anche un file che ti sembra minore o ripetitivo va collocato dove "
            "appartiene, perche' il cliente controlla proprio quelli per "
            "capire se l'analisi e' stata davvero completa.\n"
            "NON creare una sezione di raccolta in fondo al documento con i "
            "file avanzati: un file relegato in una lista finale resta senza "
            "collocazione, sparisce dalla mappa delle dipendenze e dagli altri "
            "deliverable, e il cliente non sa a cosa serva. L'unico caso in cui "
            "un file puo' restare fuori dalle sezioni e' se NON e' analizzabile "
            "(contenuto illeggibile o vuoto): in quel caso dichiaralo con il "
            "motivo tecnico preciso."
             + CONVENZIONI_FASE1
        )+ _nota_data(),
        expected_output=(
            "Un documento di 'Inventory' in Markdown: prospetto quantitativo, "
            "poi una sezione per tipologia in cui l'ANALISI in prosa precede la "
            "tabella di dettaglio, e in chiusura le criticita' trasversali. "
            "Le tabelle devono coprire tutti i file ricevuti — e' la prova che "
            "l'analisi e' stata esaustiva — ma non devono costituire la "
            "sostanza del documento: un inventario fatto di sole tabelle e' un "
            "elenco, non un assessment. "
            + vincolo_ampiezza
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
            + vincolo_ampiezza
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
            "dettagliata del software originale. "
            + vincolo_ampiezza + " "
            "VINCOLO DI PROPORZIONE: documenta ciò che hai effettivamente letto "
            "nel codice sorgente. NON aggiungere sezioni generiche sui pattern "
            "architetturali in astratto, glossari di termini informatici comuni, "
            "o descrizioni di tecnologie non presenti nel sistema. Ogni "
            "affermazione deve poter essere ricondotta a un file, una funzione o "
            "una struttura dati esistente. Se un aspetto non è deducibile dal "
            "codice, dichiaralo come lacuna invece di riempirlo con contenuto "
            "generico."
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
            + vincolo_ampiezza
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
            + vincolo_ampiezza + " "
            "VINCOLO DI PROPORZIONE: il piano di test deve essere proporzionato "
            "al sistema analizzato. NON includere sezioni di metodologia generica "
            "(cos'è un test di regressione, strategia di test in astratto, "
            "descrizione di ambienti e strumenti standard, processi di gestione "
            "dei difetti): il cliente le conosce e non le sta pagando. Ogni caso "
            "di test deve riferirsi a un comportamento SPECIFICO osservato nel "
            "codice legacy analizzato, con il nome del modulo o della funzione "
            "coinvolta. Meglio 15 casi di test verificabili che 60 pagine di "
            "metodologia."
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
            "modelli database e lo standard delle API nel nuovo sistema target.\n"
            "3. La STRUTTURA DELLA SOLUTION: l'elenco esatto dei progetti che "
            "comporranno il sistema target. E' la decisione che il cliente approva "
            "al Check Point 2, PRIMA che venga scritta una riga di codice.\n"
            + STRUTTURA_SOLUTION_RULES
            + "\n\nCODICE SORGENTE LEGACY (evidenza primaria):\n"
            "In caso di discrepanza tra la documentazione della fase precedente "
            "e il codice, fa fede il codice. Non attribuire al sistema componenti, "
            "tabelle o campi che non trovi qui.\n"
            "{contesto_sorgenti}\n"
            + MERMAID_RULES
            + blocco_contesto
        )+ _nota_data(),
     expected_output=(
            "Un documento in formato Markdown che include il Migration Plan completo "
            "e l'elenco formale degli ADR (Architectural Decision Records) per "
            "guidare lo sviluppo. "
            "VINCOLO DI PROPORZIONE: il documento deve essere proporzionato alla "
            "dimensione reale del sistema analizzato. Non includere procedure "
            "standard di project management indipendenti da questo specifico "
            "sistema (creazione repository, strategia di branching, setup "
            "dell'ambiente di sviluppo, struttura generica delle cartelle, "
            "processi di rilascio): il cliente le conosce già e non le sta "
            "pagando. Concentrati esclusivamente su ciò che è SPECIFICO di "
            "questo sistema legacy: come si traduce ogni componente esistente, "
            "quali decisioni architetturali richiede il codice che hai letto, "
            "quali rischi di migrazione presenta. "
            "VINCOLO DI COMPLETEZZA: massimo 8 sezioni principali. Il documento "
            "deve essere COMPLETO e autoconclusivo: non annunciare nell'indice "
            "sezioni che non svilupperai, non inserire rinvii a sezioni "
            "inesistenti, e concludi il testo prima di esaurire lo spazio. "
            "Meglio 5 sezioni piene di contenuto specifico che 8 riempite di "
            "prassi generiche. "
            "OBBLIGATORIO: il documento deve contenere la sezione "
            "'### STRUTTURA SOLUTION' nel formato indicato, con l'elenco puntato "
            "dei progetti. E' la parte che il cliente approva e che guida la "
            "generazione del codice: senza, la Fase 3 inventa una struttura propria."
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
            "4. Aggiungi i `CREATE INDEX` necessari per ottimizzare le query future.\n"
            "5. Apri il file con una MAPPA DI MIGRAZIONE DEI DATI, in commenti SQL, "
            "che dichiari per ogni tabella nuova da quali file/tabelle legacy deriva "
            "e con quale criterio. Usa questo formato, una riga per tabella:\n"
            "--   nuova_tabella  <--  origine1.dbf, origine2.dbf  |  criterio\n"
            "Il criterio spiega COSA e' successo: 'copia normalizzata', "
            "'fusione: teamprop portava le proprieta', teamlog lo storico', "
            "'scissione: la testata resta qui, le righe vanno in movmag_righe', "
            "'nuova: non esisteva nel legacy, serve per <motivo>'.\n"
            "Elenca in fondo alla mappa le tabelle legacy NON migrate, con il "
            "motivo (dati temporanei, duplicati, obsoleti): il cliente deve poter "
            "verificare che nulla sia sparito senza una ragione.\n"
            "Ripeti poi l'origine come commento sopra ogni singola CREATE TABLE.\n\n"
            "IMPORTANTE: l'output deve essere SOLO SQL valido (con commenti `--` "
            "dove serve), senza testo Markdown attorno, perché verrà salvato come "
            "file .sql eseguibile."
            "VINCOLO DI ADERENZA: progetta lo schema SOLO sulla base della "
            "persistenza REALMENTE presente nel sistema legacy (tabelle, file "
            "di dati, configurazioni salvate). Se il sistema analizzato non "
            "utilizza un database, dichiaralo esplicitamente e limita il "
            "documento a quella constatazione, eventualmente suggerendo se e "
            "quale persistenza avrebbe senso introdurre — senza però produrre "
            "uno schema completo non richiesto dal sistema esistente."
            "\n\nCODICE SORGENTE LEGACY (evidenza primaria):\n"
            "In caso di discrepanza tra la documentazione della fase precedente "
            "e il codice, fa fede il codice. Non attribuire al sistema componenti, "
            "tabelle o campi che non trovi qui.\n"
            "{contesto_sorgenti}\n"
            + blocco_contesto
        )+ _nota_data(),
        expected_output=(
            "Uno script SQL formattato correttamente contenente le istruzioni DDL "
            "per la creazione del nuovo database relazionale, preceduto dalla "
            "MAPPA DI MIGRAZIONE DEI DATI in commenti SQL. La mappa e' "
            "OBBLIGATORIA: senza, il cliente non ha modo di sapere che fine ha "
            "fatto ciascuna delle sue tabelle, e la migrazione dei dati diventa "
            "un lavoro di ricostruzione a posteriori."
            "VINCOLO DI COMPLETEZZA: massimo 8 sezioni principali. Il documento "
            "deve essere COMPLETO: non annunciare nell'indice sezioni che non "
            "svilupperai, e concludi il testo prima di esaurire lo spazio "
            "disponibile. Meglio 6 sezioni complete che 13 dichiarate e troncate. "
            "Non inserire rinvii a sezioni che non esistono nel documento."
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
    tipi_gia_generati=None,
    progetti_esistenti=None,
):
    """
    Genera i task dinamicamente per UN SINGOLO file legacy,
    iniettando l'architettura globale (ADR + schema DB).

    `tipi_gia_generati` sono le classi/interfacce prodotte durante i file
    precedenti: senza, ogni passata rigenera le classi condivise con nomi e
    namespace leggermente diversi (es. Dtos/ e DTOs/ come cartelle distinte).
    """

    # 🛡️ PROTEZIONE: Disinnesca le graffe dal codice legacy originale e dai report
    safe_legacy = _escape_braces(contenuto_file_legacy)
    safe_adr = _escape_braces(contesto_adr)
    safe_sql = _escape_braces(contesto_sql)
    safe_funzionale = _escape_braces(contesto_funzionale)
    safe_test = _escape_braces(contesto_test)

    if progetti_esistenti:
        elenco_prog = ", ".join(sorted(progetti_esistenti))
        blocco_progetti = f"""
        ARCHITETTURA APPROVATA DAL CLIENTE — USA QUESTI PROGETTI, NON CREARNE ALTRI:
        {_escape_braces(elenco_prog)}
        Ogni file che produci deve stare in UNO di questi progetti. NON
        inventare nomi nuovi, NON aggiungere suffissi tipo "Modernized",
        "New" o "WebApi", NON alternare punti e underscore: `Fox.Warehouse` e
        `Fox_Warehouse` diventerebbero due progetti distinti per la stessa
        cosa, e la solution non si compila piu'.
        Questa struttura e' stata decisa in fase di progettazione e APPROVATA
        dal cliente al Check Point 2: non e' una proposta, e' un vincolo.
        Solo se una funzionalita' non ha davvero posto in nessuno di essi puoi
        aggiungere un progetto, seguendo ESATTAMENTE la stessa convenzione di
        nomi di quelli elencati.
"""
    else:
        # Primo file della fase: qui l'architettura si DECIDE. I nomi scelti
        # ora diventano vincolanti per tutti i file successivi.
        blocco_progetti = f"""
        SEI IL PRIMO FILE DELLA MIGRAZIONE: definisci ORA la struttura della
        solution, e varra' per tutti i file successivi.

        REGOLA DI STRUTTURA (vale per QUALSIASI progetto, di qualsiasi
        dimensione): dividi per STRATO, non per funzionalita'. Massimo
        {MAX_PROGETTI_ATTESI} progetti in tutto, con nomi del tipo:
          <Prodotto>.Api            (endpoint e controller)
          <Prodotto>.Application    (casi d'uso, servizi applicativi)
          <Prodotto>.Domain         (entita' e regole di business)
          <Prodotto>.Infrastructure (accesso a dati e sistemi esterni)

        Le aree funzionali del sistema legacy (magazzino, personale,
        contabilita'...) diventano CARTELLE dentro questi progetti, MAI
        progetti separati: il numero di progetti non deve crescere con il
        numero di file migrati. Una sola convenzione di separatori: usa il
        punto, mai l'underscore.
"""

    if tipi_gia_generati:
        elenco = ", ".join(sorted(tipi_gia_generati)[:150])
        blocco_tipi_esistenti = f"""
        TIPI GIÀ GENERATI NEI FILE PRECEDENTI (NON RIDEFINIRLI):
        {_escape_braces(elenco)}
        Se ti serve uno di questi, RIUSALO importandolo con lo stesso nome e lo
        stesso namespace. Non crearne una variante, non cambiarne il nome, non
        duplicarne il file: sono già presenti nel progetto.
"""
    else:
        blocco_tipi_esistenti = ""

    blocco_tipi_esistenti = blocco_progetti + blocco_tipi_esistenti

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
{blocco_tipi_esistenti}
        FORMATO DI OUTPUT OBBLIGATORIO (ripetibile per ogni file generato):
        /// FILEPATH: src/backend/...
        ```
        [tuo codice pulito qui]
        ```
        Il percorso dopo /// FILEPATH: deve essere sempre RELATIVO (mai assoluto, mai contenente '..').
        """,
        expected_output=(
            f"Il codice sorgente Backend rifattorizzato per il file {nome_file_legacy}, "
            "completo di unit test, nel formato /// FILEPATH richiesto. "
            "VINCOLO DI PROPORZIONE: genera SOLO il codice che traduce questo "
            "specifico file legacy e i relativi test. NON produrre scaffolding di "
            "progetto (file di configurazione, Program.cs o entry point generici, "
            "Dockerfile, pipeline CI/CD, README, file di progetto, struttura di "
            "cartelle vuote): sono prassi standard che il cliente non sta pagando "
            "e che verrebbero rigenerate identiche per ogni file. Se il file legacy "
            "contiene poca logica, produci poco codice: la quantità deve essere "
            "proporzionata al contenuto reale dell'originale."
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

        CONFINE INVALICABILE — NON RIPRODURRE IL BACKEND:
        Il codice backend per questo file È GIÀ STATO SCRITTO dal tuo collega e ti
        viene fornito come contesto. Il tuo compito è CONSUMARLO via HTTP, non
        riscriverlo. In particolare NON devi mai emettere:
        - Controller, Service, Repository, DbContext, entità di dominio, DTO lato
          server, migrazioni, middleware o unit test del backend;
        - file sotto src/backend/ (i tuoi percorsi iniziano SEMPRE con src/frontend/).
        Se ti serve una struttura dati che il backend già espone, definisci al più
        un modello di sola vista lato client e mappalo dalla risposta JSON: non
        ricopiare la classe del server.
        Riferisciti agli endpoint del backend per URL e forma del payload, senza
        ridefinirne l'implementazione.
{blocco_tipi_esistenti}
        FORMATO DI OUTPUT OBBLIGATORIO (ripetibile per ogni file generato):
        /// FILEPATH: src/frontend/...
        ```
        [tuo codice UI qui]
        ```
        Il percorso dopo /// FILEPATH: deve essere sempre RELATIVO (mai assoluto, mai contenente '..').
        """,
        expected_output=(
            f"Il codice sorgente Frontend moderno (UI) per sostituire {nome_file_legacy}, "
            "nel formato /// FILEPATH richiesto. "
            "VINCOLO DI PROPORZIONE: genera SOLO i componenti che sostituiscono "
            "l'interfaccia di questo specifico file legacy. NON produrre "
            "boilerplate di progetto (configurazioni del bundler, package.json, "
            "temi generici, componenti di libreria, routing globale): non fanno "
            "parte della traduzione di questo file. "
            "Se il file legacy NON contiene interfaccia utente (moduli di sola "
            "logica, file di progetto, classi di utility), NON inventare una UI: "
            "dichiara in una riga che il file non ha componenti di interfaccia "
            "e concludi."
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
            "NOTA SUGLI ESTRATTI: i documenti molto lunghi ti vengono forniti "
            "parzialmente, con un marcatore esplicito nel punto di interruzione. "
            "Un estratto interrotto NON è un difetto del documento: non segnalarlo "
            "come troncamento e non bocciare per questo motivo."
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
            "Ci sono sezioni vuote o dichiarate e non sviluppate?\n"
            "   ATTENZIONE: se un documento è preceduto dall'avviso di ESTRATTO "
            "PARZIALE, ciò che vedi è solo una porzione scelta da noi per limiti "
            "di contesto — il file su disco è completo. NON segnalare come "
            "rilievo il fatto che il testo si interrompa, né che il codice "
            "risulti tagliato o non compilabile: sarebbe un difetto del nostro "
            "estratto, non del deliverable. Valuta solo ciò che PUOI vedere.\n"
            "   Se fra i documenti c'e' un inventario, verifica DUE cose: che "
            "copra tutti i file (prospetto quantitativo e tabelle di dettaglio) "
            "e che contenga ANALISI e non solo elenchi. Un inventario fatto "
            "quasi solo di tabelle e' un elenco travestito da assessment: "
            "segnalalo come rilievo, perche' il cliente paga per sapere cosa "
            "fanno i suoi file, non quali possiede. Vale anche il contrario: "
            "un'analisi che cita solo gli esempi piu' rappresentativi lascia il "
            "cliente senza sapere se il SUO file e' stato esaminato.\n"
            "   Segnala anche i file relegati in una sezione di raccolta finale "
            "senza collocazione tematica: sono citati ma non analizzati, "
            "spariscono dagli altri documenti e interrompono la tracciabilita'.\n"
            "   Se fra i documenti c'e' uno schema di database, verifica che "
            "contenga la MAPPA DI MIGRAZIONE DEI DATI e che ogni tabella legacy "
            "citata nell'analisi vi compaia — come migrata oppure come "
            "esplicitamente esclusa con motivo. Una tabella che sparisce senza "
            "spiegazione e' un rilievo grave: significa dati che il cliente "
            "scoprira' mancanti solo a migrazione avvenuta.\n"
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