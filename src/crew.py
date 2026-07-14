import os

from crewai import Crew, Process
from src.agents import create_agents
from src.tasks import get_quality_check_task, get_understanding_tasks, get_design_tasks, get_iterative_implementation_tasks

def run_understanding_phase(llm, codice_legacy, output_dir):
    """
    Esegue la FASE 1: Understanding.
    Crea l'inventario, la mappa delle dipendenze, la documentazione e il test book.
    """
    agents = create_agents(llm)
    tasks = get_understanding_tasks(agents, output_dir)
    
    # Selezioniamo solo gli agenti necessari per questa fase
    fase1_agents = [
        agents["legacy_system_analyzer"],
        agents["dependency_mapper"],
        agents["tech_business_documenter"],
        agents["functional_analyst"],
        agents["qa_test_planner"]
    ]
    
    crew = Crew(
        agents=fase1_agents,
        tasks=tasks,
        process=Process.sequential,
        verbose=True,
        memory=False # Disattivato per evitare errori di fuso orario/database locale
    )
    
    return crew.kickoff(inputs={"codice_legacy": codice_legacy})

def run_design_phase(llm, linguaggio_target, output_dir):
    """
    Esegue la FASE 2: Design.
    Prende le specifiche della fase 1 (presenti in output_dir) e genera il Migration Plan e gli ADR.
    """
    agents = create_agents(llm)
    tasks = get_design_tasks(agents, output_dir)
    
    crew = Crew(
        agents=[
            agents["cloud_solutions_architect"],
            agents["database_administrator"]
        ],
        tasks=tasks,
        process=Process.sequential,
        verbose=True,
        memory=False
    )
    
    return crew.kickoff(inputs={"linguaggio_target": linguaggio_target})

def run_implementation_phase(llm, linguaggio_target, output_dir, lista_file_legacy_estratti):
    agents = create_agents(llm)
    
    # 1. Caricamento del Contesto (ADR e Database generati precedentemente)
    percorso_adr = f"{output_dir}/5_Migration_Plan_ADR.md"
    percorso_sql = f"{output_dir}/3_Database_Schema.sql"
    
    contesto_adr = open(percorso_adr, "r", encoding="utf-8").read() if os.path.exists(percorso_adr) else "Nessun ADR."
    contesto_sql = open(percorso_sql, "r", encoding="utf-8").read() if os.path.exists(percorso_sql) else "Nessun DB Schema."
    
    # 2. Svuotamento preventivo dei file finali (evita duplicati se si lancia due volte)
    percorso_backend = f"{output_dir}/6a_Backend_Project_Implementation.md"
    percorso_frontend = f"{output_dir}/6b_Frontend_Project_Implementation.md"
    open(percorso_backend, 'w', encoding="utf-8").close()
    open(percorso_frontend, 'w', encoding="utf-8").close()

    # 3. IL CICLO ITERATIVO: Analizza un file alla volta
    for file_info in lista_file_legacy_estratti:
        tasks_iterativi = get_iterative_implementation_tasks(
            agents=agents,
            linguaggio_target=linguaggio_target,
            nome_file_legacy=file_info['nome'],
            contenuto_file_legacy=file_info['codice'],
            contesto_adr=contesto_adr,
            contesto_sql=contesto_sql
        )
        
        dev_crew = Crew(
            agents=[agents["senior_migration_developer"], agents["frontend_developer"]],
            tasks=tasks_iterativi,
            process=Process.sequential,
            verbose=False # Silenzioso per non inondare la console
        )
        
        dev_crew.kickoff()
        
# Estrai e accoda i risultati (usa .raw oppure str() per compatibilità universale)
        output_backend = getattr(tasks_iterativi[0].output, 'raw', str(tasks_iterativi[0].output))
        output_frontend = getattr(tasks_iterativi[1].output, 'raw', str(tasks_iterativi[1].output))

        with open(percorso_backend, "a", encoding="utf-8") as f:
            f.write(f"\n\n\n")
            f.write(output_backend)
            
        with open(percorso_frontend, "a", encoding="utf-8") as f:
            f.write(f"\n\n\n")
            f.write(output_frontend)

    # 4. QUALITY CHECK (Eseguito una sola volta alla fine su tutto il lavoro)
    qa_tasks = get_quality_check_task(agents=agents, output_dir=output_dir)
    qa_crew = Crew(
        agents=[agents["security_quality_reviewer"]],
        tasks=qa_tasks,
        process=Process.sequential,
        verbose=True
    )
    qa_crew.kickoff()
    
    return True