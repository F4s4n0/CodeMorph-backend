from fastapi import FastAPI, Form, File, UploadFile, Depends, HTTPException
from fastapi.responses import FileResponse
from typing import Optional, List
from pydantic import BaseModel
from pathlib import Path
from dotenv import load_dotenv
import os
import uuid
import shutil
import zipfile
from fastapi.middleware.cors import CORSMiddleware

from src.code_unpacker import unpack_markdown_to_files
from auth import get_current_user_and_validate_license

load_dotenv()

# Legge il percorso dal .env. Se non lo trova, crea una cartella "workspace" locale di fallback
WORKSPACE_DIR = Path(os.getenv("WORKSPACE_DIR", "workspace"))

# Crea la cartella principale sul tuo disco se non esiste ancora
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

from src.crew import run_understanding_phase, run_design_phase, run_implementation_phase
from src.llm_config import get_llm
from src.graph_builder import process_directory_to_graph

app = FastAPI(
    title="Piattaforma Enterprise di Modernizzazione Universale",
    description="API suddivise in fasi con Checkpoint umani (HITL) e Capability Registry.",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # In produzione metterai il dominio vero del tuo sito
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Modelli per l'input dei dati dal frontend
class InputFase1(BaseModel):
    codice_legacy: str
    provider_llm: str = "openai"
    modello_llm: str = "gpt-4o"

class InputFase2(BaseModel):
    session_id: str
    linguaggio_target: str
    provider_llm: str = "openai"
    modello_llm: str = "gpt-4o"

class InputFase3(BaseModel):
    session_id: str
    linguaggio_target: str
    provider_llm: str = "openai"
    modello_llm: str = "gpt-4o"


# --- NUOVA DIPENDENZA: VERIFICA SE L'UTENTE È UN ADMIN ---
def require_admin(user_id: str = Depends(get_current_user_and_validate_license)):
    from auth import supabase
    try:
        # Cerchiamo il ruolo dell'utente nella tabella profiles
        utente = supabase.table("profiles").select("role").eq("id", user_id).single().execute()
        
        if not utente.data or utente.data.get("role") != "admin":
            raise HTTPException(
                status_code=403, 
                detail="Accesso negato: Questa operazione richiede privilegi di Amministratore."
            )
        return user_id # Se è admin, restituisce l'ID e dà il via libera
        
    except Exception as e:
        raise HTTPException(status_code=403, detail="Impossibile verificare i privilegi di amministratore.")

# --- ENDPOINT FASE 1: UNDERSTANDING (DOPPIA MODALITA' ZIP/TESTO) ---
@app.post("/api/v1/modernize/understand")
def fase1_understand(
    provider_llm: str = Form(...),
    modello_llm: str = Form(...),
    session_id: str = Form(...),  # <-- RICEVE IL SESSION_ID DAL FRONTEND
    session_name: str = Form("Progetto Senza Nome"),
    file: Optional[UploadFile] = File(None),
    codice_legacy: Optional[str] = Form(None),
    user_id: str = Depends(get_current_user_and_validate_license)
):
    # 1. SALVATAGGIO ASINCRONO SU SUPABASE
    try:
        from auth import supabase
        
        # Facciamo un "upsert": se la sessione non esiste la crea, se esiste la aggiorna
        supabase.table("migration_sessions").upsert({
            "id": session_id,
            "user_id": user_id,
            "current_step": "input", 
            "provider_llm": provider_llm,
            "modello_llm": modello_llm,
            "session_name": session_name 
        }).execute()
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Impossibile salvare la sessione asincrona: {str(e)}")

    print(f"L'utente autorizzato {user_id} sta lavorando alla sessione {session_id}")

    # Configurazione cartella di sessione con Path nativo
    cartella_output = WORKSPACE_DIR / session_id
    cartella_output.mkdir(parents=True, exist_ok=True)

    try:
        from src.graph_builder import log_message, process_directory_to_graph
        llm = get_llm(provider=provider_llm, model_name=modello_llm)
        codice_da_analizzare = ""

        if file:
            if not file.filename.lower().endswith('.zip'):
                raise HTTPException(status_code=400, detail="Devi caricare l'intera Solution in formato .zip")
            
            cartella_sorgenti = cartella_output / "sorgenti_originali"
            cartella_sorgenti.mkdir(parents=True, exist_ok=True)
            
            log_message(session_id, "⚡ Ricezione pacchetto Solution ed estrazione in corso sul server...")
            zip_path = cartella_output / file.filename
            with open(zip_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)

            try:
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(cartella_sorgenti)
            except zipfile.BadZipFile:
                raise HTTPException(status_code=400, detail="Il file non è un archivio ZIP valido o è corrotto.")

            # Passiamo il session_id al costruttore di grafi
            codice_da_analizzare = process_directory_to_graph(cartella_sorgenti, llm, session_id)
            
        elif codice_legacy:
            log_message(session_id, "📝 Analisi dello script di testo singolo avviata...")
            codice_da_analizzare = codice_legacy

        log_message(session_id, "🤖 Avvio del Team AI per la stesura della documentazione tecnica formale...")
        run_understanding_phase(llm=llm, codice_legacy=codice_da_analizzare, output_dir=cartella_output)
        
        log_message(session_id, "🗜️ Generazione del pacchetto ZIP del codice e dei report in corso...")
        shutil.make_archive(str(WORKSPACE_DIR / f"{session_id}_fase1"), 'zip', str(cartella_output))
        
        log_message(session_id, "✨ [SUCCESS]: Fase 1 completata. Report pronti per l'ispezione umana.")
        log_message(session_id, "🔄 Reindirizzamento al Checkpoint 1...")

        return {
            "status": "success",
            "session_id": session_id,
            "url_download": f"/api/v1/modernize/download/{session_id}/1"
        }
    except Exception as e:
        from src.graph_builder import log_message
        log_message(session_id, f"❌ ERRORE CRITICO DI SISTEMA: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Errore interno IA: {str(e)}")


# --- ENDPOINT PER IL LETTORE DI LOG LIVE (CORRETTO CON WORKSPACE_DIR) ---
@app.get("/api/v1/modernize/logs/{session_id}")
def ottieni_log_live(session_id: str):
    log_path = WORKSPACE_DIR / session_id / "live_logs.txt"
    if log_path.exists():
        with open(log_path, "r", encoding="utf-8") as f:
            contenuto = f.read()
            linee_formattate = []
            for linea in contenuto.split("\n"):
                if "📦" in linea or "🗄️" in linea:
                    linee_formattate.append(f"<span style='color: #a855f7;'>&gt;</span> {linea}")
                elif "🧠" in linea:
                    linee_formattate.append(f"<span style='color: #3b82f6;'>&gt;</span> {linea}")
                elif "❌" in linea:
                    linee_formattate.append(f"<span style='color: #ef4444;'>&gt; {linea}</span>")
                elif "📈" in linea or "📦 Compilazione archivio" in linea:
                    linee_formattate.append(f"<span style='color: #22c55e;'>&gt; {linea}</span>")
                elif linea.strip():
                    linee_formattate.append(f"&gt; {linea}")
            return {"logs": "<br>".join(linee_formattate)}
    return {"logs": "Inizializzazione sessione di log..."}
        

# --- FASE 2: DESIGN (Dopo il Checkpoint 1) ---
@app.post("/api/v1/modernize/design")
def fase2_design(
    richiesta: InputFase2, 
    user_id: str = Depends(get_current_user_and_validate_license)
):
    cartella_output = WORKSPACE_DIR / richiesta.session_id
    if not cartella_output.exists():
        raise HTTPException(status_code=404, detail="Sessione non trovata. Carica prima la Fase 1.")

    try:
        # 1. AGGIORNAMENTO STATO SU SUPABASE
        from auth import supabase
        supabase.table("migration_sessions").upsert({
            "id": richiesta.session_id,
            "user_id": user_id,
            "current_step": "cp2",
            "linguaggio_target": richiesta.linguaggio_target
        }).execute()
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore salvataggio sessione Supabase: {str(e)}")

    try:
        # 2. LOGICA AGENTI AI
        llm = get_llm(provider=richiesta.provider_llm, model_name=richiesta.modello_llm)
        run_design_phase(llm=llm, linguaggio_target=richiesta.linguaggio_target, output_dir=cartella_output)
        
        # Aggiorniamo l'archivio includendo anche il Migration Plan e gli ADR (file 1-5)
        shutil.make_archive(str(WORKSPACE_DIR / f"{richiesta.session_id}_fase2"), 'zip', str(cartella_output))
        
        return {
            "status": "success",
            "messaggio": "Fase 2 (Design) completata. In attesa del CHECK POINT 2 umano per le decisioni architetturali.",
            "session_id": richiesta.session_id,
            "url_download_report": f"/api/v1/modernize/download/{richiesta.session_id}/2"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- FASE 3: IMPLEMENTATION (Dopo il Checkpoint 2 - CORRETTO ZIP FINALE) ---
@app.post("/api/v1/modernize/implement")
def fase3_implement(
    richiesta: InputFase3, 
    user_id: str = Depends(get_current_user_and_validate_license)
):
    cartella_output = WORKSPACE_DIR / richiesta.session_id
    cartella_sorgenti = cartella_output / "sorgenti_originali"
    
    if not cartella_output.exists():
        raise HTTPException(status_code=404, detail="Sessione non trovata.")

    try:
        # 1. AGGIORNAMENTO STATO SU SUPABASE
        from auth import supabase
        supabase.table("migration_sessions").upsert({
            "id": richiesta.session_id,
            "user_id": user_id,
            "current_step": "final",
            "linguaggio_target": richiesta.linguaggio_target
        }).execute()
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore salvataggio sessione Supabase: {str(e)}")

    try:
        llm = get_llm(provider=richiesta.provider_llm, model_name=richiesta.modello_llm)
        
        # PESCHIAMO I FILE LEGACY ESTRATTI NELLA FASE 1
        lista_file_legacy = []
        if cartella_sorgenti.exists():
            for root, dirs, files in os.walk(cartella_sorgenti):
                for file_name in files:
                    file_path = os.path.join(root, file_name)
                    try:
                        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                            contenuto = f.read()
                            lista_file_legacy.append({
                                "nome": file_name, 
                                "codice": contenuto
                            })
                    except Exception as e:
                        print(f"Impossibile leggere il file {file_name}: {e}")
        
        if not lista_file_legacy:
            lista_file_legacy = [{"nome": "codice_sconosciuto.txt", "codice": "Nessun codice trovato."}]

        # AVVIO MOTORE IA
        run_implementation_phase(
            llm=llm, 
            linguaggio_target=richiesta.linguaggio_target, 
            output_dir=cartella_output,
            lista_file_legacy_estratti=lista_file_legacy
        )
        
        # SCONFEZIONAMENTO MAGICO DEL CODICE GENERATO
        print("🛠️ Organizzazione del codice Backend in cartelle fisiche...")
        unpack_markdown_to_files(f"{cartella_output}/6a_Backend_Project_Implementation.md", cartella_output)
        
        print("🛠️ Organizzazione del codice Frontend in cartelle fisiche...")
        unpack_markdown_to_files(f"{cartella_output}/6b_Frontend_Project_Implementation.md", cartella_output)
        
        # ZIP FINALE PER IL CLIENTE (CORRETTO CON WORKSPACE_DIR)
        shutil.make_archive(str(WORKSPACE_DIR / f"{richiesta.session_id}_finale"), 'zip', str(cartella_output))
        
        return {
            "status": "success",
            "messaggio": "Fase 3 (Implementation) completata. Progetto pronto per il Testing & Deployment umano.",
            "session_id": richiesta.session_id,
            "url_download_progetto": f"/api/v1/modernize/download/{richiesta.session_id}/3"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    

# --- ENDPOINT DI DOWNLOAD DINAMICO (CORRETTO CON WORKSPACE_DIR) ---
@app.get("/api/v1/modernize/download/{session_id}/{fase}")
def scarica_file(session_id: str, fase: int):
    mappa_nomi = {1: "fase1", 2: "fase2", 3: "finale"}
    if fase not in mappa_nomi:
        raise HTTPException(status_code=400, detail="Fase non valida. Scegli tra 1, 2 o 3.")
        
    zip_path = WORKSPACE_DIR / f"{session_id}_{mappa_nomi[fase]}.zip"
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="File non trovato. Elabora la fase corrispondente.")
        
    return FileResponse(
        path=str(zip_path), 
        media_type="application/zip", 
        filename=f"Modernizzazione_Sessione_{session_id}_{mappa_nomi[fase]}.zip"
    )


# --- ENDPOINT UPLOAD FILE MODIFICATI (HITL) CON SICUREZZA ---
@app.post("/api/v1/modernize/upload/{session_id}")
def carica_file_modificati(session_id: str, files: List[UploadFile] = File(...)):
    cartella_output = WORKSPACE_DIR / session_id
    if not cartella_output.exists():
        raise HTTPException(status_code=404, detail="Sessione non trovata.")

    file_sovrascritti = []
    
    for file in files:
        # 🛡️ CONTROLLO DI SICUREZZA LATO SERVER
        if not file.filename.lower().endswith('.md'):
            raise HTTPException(
                status_code=400, 
                detail=f"Violazione di sicurezza: Il file '{file.filename}' non è un file Markdown consentito."
            )

        percorso_file = cartella_output / file.filename
        with open(percorso_file, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        file_sovrascritti.append(file.filename)

    return {
        "status": "success", 
        "messaggio": "File verificati e sovrascritti con successo.", 
        "file_aggiornati": file_sovrascritti
    }

# --- ENDPOINT ADMIN: RECUPERO DI TUTTI GLI UTENTI REGISTRATI ---
@app.get("/api/v1/admin/users")
def admin_ottieni_tutti_gli_utenti(
    user_id: str = Depends(require_admin) # Protetto dal controllo admin creato prima
):
    from auth import supabase
    try:
        # Prende ID ed Email di tutti gli utenti registrati nel sistema
        risposta = supabase.table("profiles").select("id", "email").execute()
        return risposta.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore nel recupero degli utenti: {str(e)}")
    
# --- ENDPOINT ADMIN: RECUPERO DI TUTTE LE SESSIONI DI TUTTI GLI UTENTI ---
@app.get("/api/v1/admin/sessions")
def admin_ottieni_tutte_le_sessioni(
    user_id: str = Depends(require_admin) # 🛡️ Ora questo endpoint è blindato!
):
    # Nota: In produzione qui controlleresti se l'user_id ha il ruolo "admin" nel tuo DB.
    from auth import supabase
    try:
        # Leggiamo tutte le sessioni ordinandole dalla più recente alla più vecchia
        risposta = supabase.table("migration_sessions").select("*").order("updated_at", desc=True).execute()
        return risposta.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore admin nel recupero sessioni: {str(e)}")


# --- ENDPOINT ADMIN: CANCELLAZIONE TOTALE (DB + FILE SYSTEM) ---
@app.delete("/api/v1/admin/sessions/{session_id}")
def admin_cancella_sessione(
    session_id: str,
    user_id: str = Depends(require_admin) # 🛡️ Blindato anche questo!
):
    from auth import supabase
    
    # 1. PULIZIA DEL FILE SYSTEM (Cartella di lavoro + ZIP orfani)
    cartella_sessione = WORKSPACE_DIR / session_id
    try:
        # Elimina la cartella del progetto e tutto il suo contenuto (sorgenti, markdown, ecc.)
        if cartella_sessione.exists() and cartella_sessione.is_dir():
            shutil.rmtree(cartella_sessione)
            print(f"🗑️ Cartella fisica eliminata: {cartella_sessione}")
        
        # Elimina i file ZIP generati per i download che risiedono nella radice del workspace
        for suffisso in ["_fase1.zip", "_fase2.zip", "_finale.zip"]:
            zip_path = WORKSPACE_DIR / f"{session_id}{suffisso}"
            if zip_path.exists():
                zip_path.unlink()
                print(f"🗑️ Archivio ZIP rimosso: {zip_path}")
                
    except Exception as e:
        # Logghiamo l'errore del file system ma andiamo avanti per non lasciare il DB disallineato
        print(f"⚠️ Errore parziale durante la rimozione dei file di {session_id}: {e}")

    # 2. CANCELLAZIONE RECORD SU SUPABASE
    try:
        supabase.table("migration_sessions").delete().eq("id", session_id).execute()
        return {
            "status": "success", 
            "messaggio": f"Sessione {session_id} e tutti i file correlati rimossi definitivamente dal server."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore durante l'eliminazione dal database: {str(e)}")