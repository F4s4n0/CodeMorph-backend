import json
import logging
import os

from crewai import Crew, Process

from src.agents import create_agents
from src.config import (
    FILE_ASSESSMENT,
    FILE_DEPENDENCY_MAP,
    FILE_TECH_DOC,
    FILE_FUNCTIONAL_DOC,
    FILE_DB_SCHEMA,
    FILE_MIGRATION_PLAN,
    FILE_BACKEND_IMPL,
    FILE_FRONTEND_IMPL,
    FILE_IMPL_CHECKPOINT,
    FILE_QUALITY_REPORT,
    QA_CHUNK_MAX_CHARS,
)
from src.live_log import crea_logger_attivita, log_message
from src.tasks import (
    get_understanding_tasks,
    get_design_tasks,
    get_iterative_implementation_tasks,
    get_quality_check_task,
)

logger = logging.getLogger(__name__)


# =====================================================================
# Helper di basso livello
# =====================================================================
def _pulisci_output(contenuto):
    """
    Rimuove l'eventuale ragionamento interno dell'agente ("Thought: ...")
    che CrewAI a volte lascia trapelare in testa all'output finale.
    Il testo vero riparte dal primo titolo Markdown ('#'); in mancanza,
    dal primo doppio a-capo. Se non troviamo un confine sicuro, meglio
    lasciare tutto com'è che rischiare di tagliare contenuto buono.
    """
    if not contenuto.lstrip().startswith("Thought:"):
        return contenuto
    posizione_titolo = contenuto.find("#")
    if posizione_titolo != -1:
        return contenuto[posizione_titolo:]
    posizione_paragrafo = contenuto.find("\n\n")
    if posizione_paragrafo != -1:
        return contenuto[posizione_paragrafo:].lstrip()
    logger.warning("Output che inizia con 'Thought:' ma senza confine chiaro: lo lascio intatto.")
    return contenuto

def _salva_output_su_disco(tasks, output_dir):
    """
    Scrive esplicitamente l'output di ogni task nella cartella di sessione.
    ATTENZIONE: non fidarsi di task.output_file per la CARTELLA — le versioni
    recenti di CrewAI sanificano i percorsi assoluti rimuovendo la '/' iniziale,
    trasformandoli in relativi (finirebbero dentro la cartella del progetto).
    Da output_file prendiamo SOLO il nome del file; la cartella è la nostra.
    """
    for task in tasks:
        percorso_task = getattr(task, "output_file", None)
        if not percorso_task:
            continue
        percorso = os.path.join(output_dir, os.path.basename(percorso_task))
        try:
            contenuto = _task_output_text(task)
            os.makedirs(output_dir, exist_ok=True)
            with open(percorso, "w", encoding="utf-8") as f:
                f.write(contenuto)
            logger.info("Output salvato: %s (%d caratteri)", percorso, len(contenuto))
        except Exception:
            logger.exception("Impossibile salvare l'output del task su %s", percorso)


def _task_output_text(task):
    """Estrae il testo dall'output di un Task (compatibile tra versioni CrewAI)."""
    return _pulisci_output(getattr(task.output, "raw", None) or str(task.output))


def _load_checkpoint(path):
    """Carica l'elenco dei file legacy già processati (per il resume dopo crash)."""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Checkpoint corrotto (%s), riparto da zero.", e)
    return set()


def _save_checkpoint(path, processed):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(processed), f, ensure_ascii=False, indent=2)


def _chunk_text(text, max_chars):
    """Divide un testo lungo in blocchi <= max_chars, spezzando su righe intere."""
    if len(text) <= max_chars:
        return [text]
    chunks, current, current_len = [], [], 0
    for line in text.splitlines(keepends=True):
        if current_len + len(line) > max_chars and current:
            chunks.append("".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append("".join(current))
    return chunks

def _read_if_exists(path, fallback):
    """Legge un file di contesto se esiste, altrimenti restituisce il fallback."""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    logger.warning("File di contesto non trovato: %s — uso fallback.", path)
    return fallback

# =====================================================================
# FASE 1 - UNDERSTANDING
# =====================================================================

def run_understanding_phase(llm, codice_legacy, output_dir, session_id=None, tracker=None):
    """
    Esegue la FASE 1: Understanding.
    Crea l'inventario, la mappa delle dipendenze, la documentazione e il test book.

    `session_id` attiva il log live dell'attività REALE degli agenti
    (via callback CrewAI); `tracker` accumula i token consumati.
    """
    os.makedirs(output_dir, exist_ok=True)

    agents = create_agents(llm)
    tasks = get_understanding_tasks(agents, output_dir)
    annuncia_avvio, task_callback = crea_logger_attivita(
        session_id, tasks, etichetta="Fase 1 · Understanding"
    )

    # Selezioniamo solo gli agenti necessari per questa fase
    fase1_agents = [
        agents["legacy_system_analyzer"],
        agents["dependency_mapper"],
        agents["tech_business_documenter"],
        agents["functional_analyst"],
        agents["qa_test_planner"],
    ]

    crew = Crew(
        agents=fase1_agents,
        tasks=tasks,
        process=Process.sequential,
        verbose=True,
        memory=False,  # Disattivato per evitare errori di fuso orario/database locale
        task_callback=task_callback,  # Log live sincronizzato con l'attività reale
    )

    annuncia_avvio()
    risultato = crew.kickoff(inputs={"codice_legacy": codice_legacy})
    _salva_output_su_disco(tasks, output_dir)
    if tracker is not None:
        tracker.aggiungi_crew(crew, risultato)
    return risultato


# =====================================================================
# FASE 2 - DESIGN
# =====================================================================

def run_design_phase(llm, linguaggio_target, output_dir, session_id=None, tracker=None):
    """
    Esegue la FASE 2: Design.
    Rilegge da disco i documenti validati della fase 1 (le Crew sono isolate
    tra loro, quindi il contesto va reiniettato esplicitamente) e genera il
    Migration Plan, gli ADR e lo schema del database target.
    """
    os.makedirs(output_dir, exist_ok=True)

    # Contesto della FASE 1: senza questo il Cloud Architect e il DBA
    # lavorerebbero "al buio", basandosi solo sul nome del linguaggio target.
    documenti_fase1 = []
    for nome in (FILE_ASSESSMENT, FILE_DEPENDENCY_MAP, FILE_TECH_DOC, FILE_FUNCTIONAL_DOC):
        contenuto = _read_if_exists(f"{output_dir}/{nome}", "")
        if contenuto:
            documenti_fase1.append(f"### {nome}\n{contenuto}")
    contesto_fase1 = "\n\n".join(documenti_fase1)

    agents = create_agents(llm)
    tasks = get_design_tasks(agents, output_dir, contesto_fase1=contesto_fase1)
    annuncia_avvio, task_callback = crea_logger_attivita(
        session_id, tasks, etichetta="Fase 2 · Design"
    )

    crew = Crew(
        agents=[
            agents["cloud_solutions_architect"],
            agents["database_administrator"],
        ],
        tasks=tasks,
        process=Process.sequential,
        verbose=True,
        memory=False,
        task_callback=task_callback,  # Log live sincronizzato con l'attività reale
    )

    annuncia_avvio()
    risultato = crew.kickoff(inputs={
        "linguaggio_target": linguaggio_target,
        "contesto_fase1": contesto_fase1 or "Nessun documento di Fase 1 disponibile.",
    })
    _salva_output_su_disco(tasks, output_dir)
    if tracker is not None:
        tracker.aggiungi_crew(crew, risultato)
    return risultato


# =====================================================================
# FASE 3 - IMPLEMENTATION (iterativa) + QUALITY CHECK
# =====================================================================

def run_implementation_phase(
    llm,
    linguaggio_target,
    output_dir,
    lista_file_legacy_estratti,
    session_id=None,
    tracker=None,
):
    """
    Esegue la FASE 3: per ogni file legacy genera backend e frontend target,
    poi lancia il Quality Check finale sul codice prodotto.

    Ritorna un dizionario con l'esito per file e il verdetto QA, così il
    chiamante può loggare/notificare senza dover rileggere i file da disco.

    `session_id` attiva il log live per file e per task (attività reale);
    `tracker` accumula i token consumati da tutte le crew della fase.
    """
    os.makedirs(output_dir, exist_ok=True)
    agents = create_agents(llm)

    # 1. Caricamento del contesto (ADR e schema DB generati in FASE 2)
    contesto_adr = _read_if_exists(f"{output_dir}/{FILE_MIGRATION_PLAN}", "Nessun ADR.")
    contesto_sql = _read_if_exists(f"{output_dir}/{FILE_DB_SCHEMA}", "Nessun DB Schema.")

    percorso_backend = f"{output_dir}/{FILE_BACKEND_IMPL}"
    percorso_frontend = f"{output_dir}/{FILE_FRONTEND_IMPL}"
    percorso_checkpoint = f"{output_dir}/{FILE_IMPL_CHECKPOINT}"

    # 2. Checkpoint: se una run precedente è crashata a metà, riprendiamo
    #    da dove eravamo invece di rigenerare (e ripagare) tutto.
    processati = _load_checkpoint(percorso_checkpoint)
    if not processati:
        # Prima run (o restart pulito): svuota i file finali per evitare duplicati
        open(percorso_backend, "w", encoding="utf-8").close()
        open(percorso_frontend, "w", encoding="utf-8").close()
    else:
        logger.info(
            "Resume: %d file già processati verranno saltati.", len(processati)
        )
        log_message(
            session_id,
            f"⏭️ Resume: {len(processati)} file già processati in una run precedente verranno saltati.",
        )

    esiti = {"completati": [], "falliti": [], "saltati": []}
    totale = len(lista_file_legacy_estratti)
    log_message(
        session_id,
        f"⚙️ Fase 3: {totale} file legacy in coda di migrazione verso {linguaggio_target}.",
    )

    # 3. IL CICLO ITERATIVO: un file legacy alla volta
    for indice, file_info in enumerate(lista_file_legacy_estratti, start=1):
        nome_file = file_info["nome"]

        if nome_file in processati:
            logger.info("Salto %s: già processato in una run precedente.", nome_file)
            log_message(session_id, f"⏭️ ({indice}/{totale}) Salto {nome_file}: già processato.")
            esiti["saltati"].append(nome_file)
            continue

        impl_tasks = get_iterative_implementation_tasks(
            agents=agents,
            linguaggio_target=linguaggio_target,
            nome_file_legacy=nome_file,
            contenuto_file_legacy=file_info["codice"],
            contesto_adr=contesto_adr,
            contesto_sql=contesto_sql,
        )

        annuncia_avvio, task_callback = crea_logger_attivita(
            session_id,
            impl_tasks.as_list(),
            etichetta=f"file {indice}/{totale}: {nome_file}",
        )
        dev_crew = Crew(
            agents=[agents["senior_migration_developer"], agents["frontend_developer"]],
            tasks=impl_tasks.as_list(),
            process=Process.sequential,
            verbose=False,  # Silenzioso per non inondare la console
            memory=False,
            task_callback=task_callback,  # Il log live segue il lavoro reale sul file
        )

        try:
            logger.info("Migrazione di %s in corso...", nome_file)
            log_message(session_id, f"📦 ({indice}/{totale}) Migrazione di {nome_file} avviata...")
            annuncia_avvio()
            dev_crew.kickoff()
            if tracker is not None:
                tracker.aggiungi_crew(dev_crew)

            # Accesso NOMINATO agli output: niente più tasks[0]/tasks[1]
            output_backend = _task_output_text(impl_tasks.backend)
            output_frontend = _task_output_text(impl_tasks.frontend)

            with open(percorso_backend, "a", encoding="utf-8") as f:
                f.write(f"\n\n<!-- ===== ORIGINE LEGACY: {nome_file} ===== -->\n\n")
                f.write(output_backend)

            with open(percorso_frontend, "a", encoding="utf-8") as f:
                f.write(f"\n\n<!-- ===== ORIGINE LEGACY: {nome_file} ===== -->\n\n")
                f.write(output_frontend)

            # Checkpoint SOLO dopo la scrittura riuscita su entrambi i file
            processati.add(nome_file)
            _save_checkpoint(percorso_checkpoint, processati)
            esiti["completati"].append(nome_file)
            log_message(
                session_id,
                f"✅ ({indice}/{totale}) {nome_file} migrato e salvato nei file di implementazione.",
            )

        except Exception:
            # Un fallimento su un file (rate limit, timeout, errore LLM) non deve
            # bruciare il lavoro fatto sugli altri: logga e prosegui.
            logger.exception("Errore durante la migrazione di %s — proseguo.", nome_file)
            log_message(
                session_id,
                f"❌ Migrazione di {nome_file} fallita — proseguo con il file successivo.",
            )
            esiti["falliti"].append(nome_file)

    # 4. QUALITY CHECK finale, a blocchi per non saturare la context window
    codice_backend = _read_if_exists(percorso_backend, "")
    codice_frontend = _read_if_exists(percorso_frontend, "")
    codice_completo = (
        f"===== CODICE BACKEND GENERATO =====\n{codice_backend}\n\n"
        f"===== CODICE FRONTEND GENERATO =====\n{codice_frontend}"
    )

    chunks = _chunk_text(codice_completo, QA_CHUNK_MAX_CHARS)
    report_qa = []
    log_message(
        session_id,
        "🕵️ Avvio Quality Check (OWASP/SonarQube) sul codice generato"
        + (f" in {len(chunks)} parti..." if len(chunks) > 1 else "..."),
    )

    for i, chunk in enumerate(chunks, start=1):
        etichetta = f"parte {i}/{len(chunks)}" if len(chunks) > 1 else ""
        nome_report = (
            FILE_QUALITY_REPORT
            if len(chunks) == 1
            else FILE_QUALITY_REPORT.replace(".md", f"_part{i}.md")
        )

        qa_tasks = get_quality_check_task(
            agents=agents,
            output_dir=output_dir,
            codice_da_analizzare=chunk,
            chunk_label=etichetta,
            output_filename=nome_report,
        )
        annuncia_qa, callback_qa = crea_logger_attivita(
            session_id, qa_tasks, etichetta=f"Quality Check {etichetta}".strip()
        )
        qa_crew = Crew(
            agents=[agents["security_quality_reviewer"]],
            tasks=qa_tasks,
            process=Process.sequential,
            verbose=True,
            memory=False,
            task_callback=callback_qa,
        )

        try:
            annuncia_qa()
            risultato = qa_crew.kickoff()
            _salva_output_su_disco(qa_tasks, output_dir)
            if tracker is not None:
                tracker.aggiungi_crew(qa_crew, risultato)
            report_qa.append(getattr(risultato, "raw", None) or str(risultato))
        except Exception:
            logger.exception("Errore durante il Quality Check (%s).", etichetta or "unico")
            log_message(session_id, f"❌ Quality Check fallito ({etichetta or 'unico'}).")
            report_qa.append(f"QUALITY CHECK FALLITO ({etichetta or 'unico'})")

    esiti["quality_report"] = "\n\n---\n\n".join(report_qa)
    log_message(
        session_id,
        f"📈 Fase 3: pipeline completata — {len(esiti['completati'])} migrati, "
        f"{len(esiti['falliti'])} falliti, {len(esiti['saltati'])} saltati.",
    )
    return esiti
