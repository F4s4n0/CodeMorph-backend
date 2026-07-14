import os
import json
import networkx as nx
from crewai import Agent, Task, Crew
from dbfread import DBF  # <-- Libreria nativa per FoxPro

# --- LISTE DI FILTRAGGIO AGGIORNATE ---
ESCLUDI_CARTELLE = {'.git', 'node_modules', 'vendor', 'venv', 'env', '__pycache__', 'bin', 'obj', 'dist', 'build', '.idea', '.vscode'}

# Includiamo le estensioni native di Visual FoxPro (.scx per le form, .dbf per le tabelle, .prg per i programmi di codice)
ESTENSIONI_VALIDE = {
    '.php', '.py', '.js', '.ts', '.java', '.cs', '.html', '.css', '.cpp', '.c', '.h', '.go', '.rs', '.sql', '.json',
    '.prg', '.scx', '.dbf'  # <-- Estensioni FoxPro
}

MAX_FILE_SIZE = 250 * 1024 


def extract_foxpro_scx_code(file_path):
    """
    Estrae da una Form FoxPro (.scx) sia il CODICE (metodi) sia il LAYOUT GRAFICO (proprietà).
    Cruciale per permettere all'IA di ricreare la UX in tecnologie come C# WinForms.
    """
    try:
        table = DBF(file_path, ignore_missing_memofile=True, char_decode_errors='ignore')
        codice_form_estratto = []
        
        for record in table:
            # Recuperiamo sia i metodi (codice) che le properties (design/UX)
            metodi = record.get('METHODS') or record.get('methods') or ""
            proprieta = record.get('PROPERTIES') or record.get('properties') or ""
            
            nome_oggetto = record.get('OBJNAME') or record.get('objname') or "OggettoSconosciuto"
            classe_oggetto = record.get('CLASS') or record.get('class') or "ClasseSconosciuta"
            
            # Se l'oggetto ha del codice o delle impostazioni grafiche, lo salviamo
            if metodi.strip() or proprieta.strip():
                codice_form_estratto.append(f"\n==========================================")
                codice_form_estratto.append(f"*** OGGETTO: {nome_oggetto} | CLASSE: {classe_oggetto} ***")
                
                # Aggiunge le coordinate e le impostazioni grafiche/data binding
                if proprieta.strip():
                    codice_form_estratto.append("--- PROPRIETA' UI (Layout & Bindings) ---")
                    codice_form_estratto.append(proprieta.strip())
                
                # Aggiunge la logica di business FoxPro
                if metodi.strip():
                    codice_form_estratto.append("--- METODI E CODICE SORGENTE ---")
                    codice_form_estratto.append(metodi.strip())
                
        if codice_form_estratto:
            return "\n".join(codice_form_estratto)
        return f"Form {os.path.basename(file_path)} analizzata, ma vuota."
    except Exception as e:
        return f"🚨 Errore durante il parsing nativo della Form FoxPro (.scx): {str(e)}"

def extract_foxpro_dbf_schema(file_path):
    """
    Estrae SOLO lo schema (struttura dei campi) di un database .dbf senza caricare i dati.
    Previene il sovraccarico di token causato da tabelle con megabyte di record.
    """
    try:
        # load=False indica a dbfread di NON caricare i record in memoria. Legge solo l'header!
        table = DBF(file_path, load=False, char_decode_errors='ignore')
        
        schema_info = [f"--- SCHEMA TABELLA FOXPRO LEGACY: {os.path.basename(file_path)} ---"]
        for field in table.fields:
            # field.type indica il tipo di dato FoxPro (C=Character, N=Numeric, D=Date, M=Memo, ecc.)
            schema_info.append(f"  - Campo: {field.name} | Tipo FoxPro: {field.type} | Lunghezza: {field.length}")
            
        schema_info.append("--- FINE SCHEMA ---\n")
        return "\n".join(schema_info)
    except Exception as e:
        return f"🚨 Errore durante l'estrazione dello schema DBF: {str(e)}"


def extract_dependencies_from_file(file_name, file_content, llm):
    """
    Usa un Agente 'Micro' per leggere il contenuto (codice o schema) e restituire un JSON strutturato.
    """
    extractor_agent = Agent(
        role='Dependency Extractor',
        goal='Estrarre le dipendenze strutturali software o database e restituire un JSON valido.',
        backstory="Sei un analista sintattico specializzato in refactoring e reverse engineering, incluso codice legacy Visual FoxPro.",
        llm=llm,
        verbose=False
    )

    extraction_task = Task(
        description=(
            f"Analizza l'entità denominata '{file_name}'. Il suo contenuto o schema estratto è il seguente:\n\n{file_content}\n\n"
            "Identifica le dipendenze (altre tabelle chiamate, altre form invocate tramite DO FORM, o file esterni).\n"
            "Restituisci SOLO un oggetto JSON con questo esatto formato:\n"
            "{\n"
            '  "file": "nome_di_questo_file",\n'
            '  "depends_on": ["tabella_db_o_form_collegata_1", "entita_collegata_2"]\n'
            "}\n"
            "Se non rilevi dipendenze esplicite, restituisci una lista vuota []."
        ),
        expected_output="Una stringa JSON formattata correttamente.",
        agent=extractor_agent
    )

    crew = Crew(agents=[extractor_agent], tasks=[extraction_task])
    result = crew.kickoff()
    
    testo_pulito = result.raw.replace('```json', '').replace('```', '').strip()
    try:
        return json.loads(testo_pulito)
    except json.JSONDecodeError:
        return {"file": file_name, "depends_on": []}

def log_message(session_id, message):
    """Stampa il log sulla console server e lo scrive nel file live per il frontend."""
    print(message)
    log_dir = f"workspace/{session_id}"
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "live_logs.txt")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(message + "\n")

def process_directory_to_graph(cartella_sorgente, llm, session_id):  # <-- AGGIUNTO session_id
    """Itera sui file applicando filtri avanzati e scrive i log in tempo reale."""
    G = nx.DiGraph() 

    for root, dirs, files in os.walk(cartella_sorgente):
        dirs[:] = [d for d in dirs if d not in ESCLUDI_CARTELLE]
        
        for file in files:
            file_path = os.path.join(root, file)
            estensione = os.path.splitext(file)[1].lower()
            
            if estensione not in ESTENSIONI_VALIDE:
                continue 

            content = ""
            
            if estensione == '.scx':
                log_message(session_id, f"📦 Compilazione ed estrazione metodi dalla Form FoxPro: {file} ...")
                content = extract_foxpro_scx_code(file_path)
                
            elif estensione == '.dbf':
                log_message(session_id, f"🗄️ Estrazione chirurgica dello schema della Tabella FoxPro: {file} (Salto i record dati)...")
                content = extract_foxpro_dbf_schema(file_path)
                
            else:
                if os.path.getsize(file_path) > MAX_FILE_SIZE:
                    log_message(session_id, f"⚠️ Salto {file}: file di testo troppo grande (>250KB).")
                    continue
                try:
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                except Exception as e:
                    log_message(session_id, f"❌ Impossibile leggere il file {file}: {e}")
                    continue

            try:
                log_message(session_id, f"🧠 Analisi dipendenze IA per: {file} ...")
                dati_json = extract_dependencies_from_file(file, content, llm)
                nodo_principale = dati_json.get("file", file)
                G.add_node(nodo_principale)
                
                for dipendenza in dati_json.get("depends_on", []):
                    G.add_edge(nodo_principale, dipendenza) 
            except Exception as e:
                log_message(session_id, f"❌ Errore IA su {file}: {e}")

    log_message(session_id, "📈 Calcolo delle dipendenze strutturali completato. Generazione report...")
    gradi_ingresso = dict(G.in_degree())
    nodi_ordinati = sorted(gradi_ingresso.items(), key=lambda item: item[1], reverse=True)
    
    report_grafo = "REPORT GRAFO DELLE DIPENDENZE E SCHEMI DATABASE (VISUAL FOXPRO DETECTED):\n\n"
    for nodo, conteggio in nodi_ordinati:
        se_dipende_da = list(G.successors(nodo))
        report_grafo += f"- Modulo/File: {nodo}\n"
        report_grafo += f"  Importanza relazionale: rilevato in {conteggio} flussi software.\n"
        if se_dipende_da:
            report_grafo += f"  Interagisce/Dipende da: {', '.join(se_dipende_da)}\n"
        report_grafo += "\n"

    return report_grafo
    """
    Itera sui file applicando filtri avanzati per codice standard e parser nativi per FoxPro.
    Constructs the knowledge graph.
    """
    G = nx.DiGraph() 

    for root, dirs, files in os.walk(cartella_sorgente):
        dirs[:] = [d for d in dirs if d not in ESCLUDI_CARTELLE]
        
        for file in files:
            file_path = os.path.join(root, file)
            estensione = os.path.splitext(file)[1].lower()
            
            if estensione not in ESTENSIONI_VALIDE:
                continue 

            content = ""
            
            # --- CASO 1: LA FORM FOXPRO (.scx) ---
            if estensione == '.scx':
                print(f"📦 Compilazione ed estrazione metodi dalla Form FoxPro: {file} ...")
                content = extract_foxpro_scx_code(file_path)
                
            # --- CASO 2: IL DATABASE FOXPRO (.dbf) ---
            elif estensione == '.dbf':
                print(f"🗄️ Estrazione chirurgica dello schema della Tabella FoxPro: {file} (Salto i record dati)...")
                content = extract_foxpro_dbf_schema(file_path)
                
            # --- CASO 3: FILE DI CODICE TRADIZIONALI (.prg FoxPro, .py, .php, ecc.) ---
            else:
                if os.path.getsize(file_path) > MAX_FILE_SIZE:
                    print(f"⚠️ Salto {file}: file di testo troppo grande (>250KB).")
                    continue
                try:
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                except Exception as e:
                    print(f"❌ Impossibile leggere il file {file}: {e}")
                    continue

            # Invia il contenuto estratto (o lo schema) all'agente per mappare il Grafo
            try:
                dati_json = extract_dependencies_from_file(file, content, llm)
                nodo_principale = dati_json.get("file", file)
                G.add_node(nodo_principale)
                
                for dipendenza in dati_json.get("depends_on", []):
                    G.add_edge(nodo_principale, dipendenza) 
            except Exception as e:
                print(f"❌ Errore IA su {file}: {e}")

    print("📈 Calcolo delle dipendenze strutturali completato.")
    gradi_ingresso = dict(G.in_degree())
    nodi_ordinati = sorted(gradi_ingresso.items(), key=lambda item: item[1], reverse=True)
    
    report_grafo = "REPORT GRAFO DELLE DIPENDENZE E SCHEMI DATABASE (VISUAL FOXPRO DETECTED):\n\n"
    for nodo, conteggio in nodi_ordinati:
        se_dipende_da = list(G.successors(nodo))
        report_grafo += f"- Modulo/File: {nodo}\n"
        report_grafo += f"  Importanza relazionale: rilevato in {conteggio} flussi software.\n"
        if se_dipende_da:
            report_grafo += f"  Interagisce/Dipende da: {', '.join(se_dipende_da)}\n"
        report_grafo += "\n"

    return report_grafo