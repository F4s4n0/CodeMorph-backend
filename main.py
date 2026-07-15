import logging
import os
import re
import shutil
import zipfile
from pathlib import Path
from typing import Optional, List

from dotenv import load_dotenv
from fastapi import FastAPI, Form, File, UploadFile, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from auth import get_current_user_and_validate_license, supabase
from src.code_unpacker import unpack_markdown_to_files
from src.config import FILE_BACKEND_IMPL, FILE_FRONTEND_IMPL
from src.crew import run_understanding_phase, run_design_phase, run_implementation_phase
from src.graph_builder import (
    ESTENSIONI_VALIDE,
    extract_foxpro_dbf_schema,
    extract_foxpro_scx_code,
    log_message,
    process_directory_to_graph,
)
from src.llm_config import get_llm

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Legge il percorso dal .env; fallback su una cartella locale
WORKSPACE_DIR = Path(os.getenv("WORKSPACE_DIR", "/tmp/workspace_sessioni"))
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="Piattaforma Enterprise di Modernizzazione Universale",
    description="API suddivise in fasi con Checkpoint umani (HITL) e Capability Registry.",
    version="2.1.0",
)

# NOTA CORS: allow_origins=["*"] insieme ad allow_credentials=True viene
# rifiutato dai browser (lo standard lo vieta). In produzione sostituire
# con la lista esplicita dei domini del frontend.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =====================================================================
# Modelli di input
# =====================================================================

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


# =====================================================================
# Helper di sicurezza
# =====================================================================

# Il session_id viene usato per costruire percorsi su disco: senza questa
# validazione un valore come "../../altro_utente" permetterebbe di leggere,
# scrivere o CANCELLARE (endpoint admin delete!) cartelle arbitrarie.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _valida_session_id(session_id: str) -> str:
    if not _SESSION_ID_RE.match(session_id or ""):
        raise HTTPException(status_code=400, detail="session_id non valido.")
    return session_id


def _cartella_sessione(session_id: str) -> Path:
    """Percorso della sessione, con session_id già validato."""
    return WORKSPACE_DIR / _valida_session_id(session_id)


def _verifica_proprieta_sessione(session_id: str, user_id: str):
    """
    Impedisce a un utente autenticato di operare sulle sessioni di un ALTRO
    utente semplicemente indovinandone/conoscendone l'ID.
    Se la sessione non esiste ancora (prima chiamata di Fase 1) passa.
    """
    try:
        risposta = (
            supabase.table("migration_sessions")
            .select("user_id")
            .eq("id", session_id)
            .execute()
        )
    except Exception as e:
        logger.error("Errore verifica proprietà sessione %s: %s", session_id, e)
        raise HTTPException(status_code=503, detail="Servizio sessioni non disponibile.")

    if risposta.data and risposta.data[0]["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Questa sessione appartiene a un altro account.")


def _estrai_zip_sicuro(zip_path: Path, destinazione: Path):
    """
    Estrazione ZIP con protezione zip-slip: un archivio malevolo può contenere
    voci tipo '../../etc/cron.d/x' che extractall scriverebbe FUORI dalla
    cartella di destinazione. Ogni voce viene validata prima dell'estrazione.
    """
    destinazione_abs = destinazione.resolve()
    with zipfile.ZipFile(zip_path, "r") as zip_ref:
        for membro in zip_ref.infolist():
            percorso_finale = (destinazione_abs / membro.filename).resolve()
            if not str(percorso_finale).startswith(str(destinazione_abs) + os.sep) \
                    and percorso_finale != destinazione_abs:
                raise HTTPException(
                    status_code=400,
                    detail=f"Archivio ZIP rifiutato: percorso non sicuro ({membro.filename}).",
                )
        zip_ref.extractall(destinazione_abs)


def require_admin(user_id: str = Depends(get_current_user_and_validate_license)):
    """Verifica che l'utente autenticato abbia ruolo admin nella tabella profiles."""
    try:
        utente = supabase.table("profiles").select("role").eq("id", user_id).single().execute()
    except Exception as e:
        logger.error("Errore verifica ruolo admin per %s: %s", user_id, e)
        raise HTTPException(status_code=503, detail="Impossibile verificare i privilegi.")

    # NOTA: questo controllo sta FUORI dal try. Nella versione precedente
    # l'HTTPException 403 veniva catturata dall'except generico e riconvertita,
    # perdendo il messaggio corretto.
    if not utente.data or utente.data.get("role") != "admin":
        raise HTTPException(
            status_code=403,
            detail="Accesso negato: questa operazione richiede privilegi di Amministratore.",
        )
    return user_id


# =====================================================================
# FASE 1: UNDERSTANDING (doppia modalità ZIP / testo)
# =====================================================================

@app.post("/api/v1/modernize/understand")
def fase1_understand(
    provider_llm: str = Form(...),
    modello_llm: str = Form(...),
    session_id: str = Form(...),
    session_name: str = Form("Progetto Senza Nome"),
    file: Optional[UploadFile] = File(None),
    codice_legacy: Optional[str] = Form(None),
    user_id: str = Depends(get_current_user_and_validate_license),
):
    session_id = _valida_session_id(session_id)
    _verifica_proprieta_sessione(session_id, user_id)

    # Validazione input PRIMA di toccare DB e filesystem: senza sorgenti
    # la pipeline girerebbe a vuoto producendo documentazione del nulla.
    if not file and not (codice_legacy and codice_legacy.strip()):
        raise HTTPException(
            status_code=400,
            detail="Fornisci un archivio .zip della Solution oppure il codice legacy come testo.",
        )

    # 1. Salvataggio sessione su Supabase (upsert: crea o aggiorna)
    try:
        supabase.table("migration_sessions").upsert({
            "id": session_id,
            "user_id": user_id,
            "current_step": "input",
            "provider_llm": provider_llm,
            "modello_llm": modello_llm,
            "session_name": session_name,
        }).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Impossibile salvare la sessione: {e}")

    logger.info("Utente %s al lavoro sulla sessione %s", user_id, session_id)

    cartella_output = _cartella_sessione(session_id)
    cartella_output.mkdir(parents=True, exist_ok=True)

    try:
        llm = get_llm(provider=provider_llm, model_name=modello_llm)
        codice_da_analizzare = ""

        if file:
            if not file.filename.lower().endswith(".zip"):
                raise HTTPException(status_code=400, detail="Devi caricare l'intera Solution in formato .zip")

            cartella_sorgenti = cartella_output / "sorgenti_originali"
            cartella_sorgenti.mkdir(parents=True, exist_ok=True)

            log_message(session_id, "⚡ Ricezione pacchetto Solution ed estrazione in corso sul server...")
            zip_path = cartella_output / "solution_upload.zip"  # nome fisso: il filename del client non è affidabile
            with open(zip_path, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)

            try:
                _estrai_zip_sicuro(zip_path, cartella_sorgenti)
            except zipfile.BadZipFile:
                raise HTTPException(status_code=400, detail="Il file non è un archivio ZIP valido o è corrotto.")

            codice_da_analizzare = process_directory_to_graph(cartella_sorgenti, llm, session_id)

        else:
            log_message(session_id, "📝 Analisi dello script di testo singolo avviata...")
            codice_da_analizzare = codice_legacy

        log_message(session_id, "🤖 Avvio del Team AI per la stesura della documentazione tecnica formale...")
        run_understanding_phase(llm=llm, codice_legacy=codice_da_analizzare, output_dir=str(cartella_output))

        log_message(session_id, "🗜️ Generazione del pacchetto ZIP del codice e dei report in corso...")
        shutil.make_archive(str(WORKSPACE_DIR / f"{session_id}_fase1"), "zip", str(cartella_output))

        log_message(session_id, "✨ [SUCCESS]: Fase 1 completata. Report pronti per l'ispezione umana.")
        log_message(session_id, "🔄 Reindirizzamento al Checkpoint 1...")

        return {
            "status": "success",
            "session_id": session_id,
            "url_download": f"/api/v1/modernize/download/{session_id}/1",
        }
    except HTTPException:
        raise  # Non mascherare i 400/403 con un 500 generico
    except Exception as e:
        log_message(session_id, f"❌ ERRORE CRITICO DI SISTEMA: {e}")
        logger.exception("Errore in Fase 1, sessione %s", session_id)
        raise HTTPException(status_code=500, detail=f"Errore interno IA: {e}")


# =====================================================================
# LOG LIVE
# =====================================================================

@app.get("/api/v1/modernize/logs/{session_id}")
def ottieni_log_live(
    session_id: str,
    user_id: str = Depends(get_current_user_and_validate_license),
):
    session_id = _valida_session_id(session_id)
    _verifica_proprieta_sessione(session_id, user_id)

    log_path = WORKSPACE_DIR / session_id / "live_logs.txt"
    if not log_path.exists():
        return {"logs": "Inizializzazione sessione di log..."}

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
        elif "📈" in linea:
            linee_formattate.append(f"<span style='color: #22c55e;'>&gt; {linea}</span>")
        elif linea.strip():
            linee_formattate.append(f"&gt; {linea}")
    return {"logs": "<br>".join(linee_formattate)}


# =====================================================================
# FASE 2: DESIGN (dopo il Checkpoint 1)
# =====================================================================

@app.post("/api/v1/modernize/design")
def fase2_design(
    richiesta: InputFase2,
    user_id: str = Depends(get_current_user_and_validate_license),
):
    session_id = _valida_session_id(richiesta.session_id)
    _verifica_proprieta_sessione(session_id, user_id)

    cartella_output = _cartella_sessione(session_id)
    if not cartella_output.exists():
        raise HTTPException(status_code=404, detail="Sessione non trovata. Carica prima la Fase 1.")

    try:
        supabase.table("migration_sessions").upsert({
            "id": session_id,
            "user_id": user_id,
            "current_step": "cp2",
            "linguaggio_target": richiesta.linguaggio_target,
        }).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore salvataggio sessione Supabase: {e}")

    try:
        llm = get_llm(provider=richiesta.provider_llm, model_name=richiesta.modello_llm)
        run_design_phase(llm=llm, linguaggio_target=richiesta.linguaggio_target, output_dir=str(cartella_output))

        shutil.make_archive(str(WORKSPACE_DIR / f"{session_id}_fase2"), "zip", str(cartella_output))

        return {
            "status": "success",
            "messaggio": "Fase 2 (Design) completata. In attesa del CHECK POINT 2 umano.",
            "session_id": session_id,
            "url_download_report": f"/api/v1/modernize/download/{session_id}/2",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Errore in Fase 2, sessione %s", session_id)
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
# FASE 3: IMPLEMENTATION (dopo il Checkpoint 2)
# =====================================================================

def _carica_file_legacy(cartella_sorgenti: Path):
    """
    Raccoglie i file legacy da migrare riusando la STESSA strategia di
    estrazione della Fase 1: parser nativi per .scx/.dbf e filtro sulle
    estensioni note. Prima venivano letti TUTTI i file come testo
    (binari .dbf inclusi, con errors='ignore'): il developer riceveva
    spazzatura binaria al posto dello schema.
    """
    lista = []
    if not cartella_sorgenti.exists():
        return lista

    for root, _dirs, files in os.walk(cartella_sorgenti):
        for file_name in files:
            estensione = os.path.splitext(file_name)[1].lower()
            if estensione not in ESTENSIONI_VALIDE:
                continue

            file_path = os.path.join(root, file_name)
            try:
                if estensione == ".scx":
                    contenuto = extract_foxpro_scx_code(file_path)
                elif estensione == ".dbf":
                    contenuto = extract_foxpro_dbf_schema(file_path)
                else:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        contenuto = f.read()
                lista.append({"nome": file_name, "codice": contenuto})
            except Exception as e:
                logger.warning("Impossibile leggere il file %s: %s", file_name, e)
    return lista


@app.post("/api/v1/modernize/implement")
def fase3_implement(
    richiesta: InputFase3,
    user_id: str = Depends(get_current_user_and_validate_license),
):
    session_id = _valida_session_id(richiesta.session_id)
    _verifica_proprieta_sessione(session_id, user_id)

    cartella_output = _cartella_sessione(session_id)
    cartella_sorgenti = cartella_output / "sorgenti_originali"

    if not cartella_output.exists():
        raise HTTPException(status_code=404, detail="Sessione non trovata.")

    try:
        supabase.table("migration_sessions").upsert({
            "id": session_id,
            "user_id": user_id,
            "current_step": "final",
            "linguaggio_target": richiesta.linguaggio_target,
        }).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore salvataggio sessione Supabase: {e}")

    try:
        llm = get_llm(provider=richiesta.provider_llm, model_name=richiesta.modello_llm)

        lista_file_legacy = _carica_file_legacy(cartella_sorgenti)
        if not lista_file_legacy:
            lista_file_legacy = [{"nome": "codice_sconosciuto.txt", "codice": "Nessun codice trovato."}]

        # AVVIO MOTORE IA: ora ritorna gli esiti (completati/falliti/QA)
        esiti = run_implementation_phase(
            llm=llm,
            linguaggio_target=richiesta.linguaggio_target,
            output_dir=str(cartella_output),
            lista_file_legacy_estratti=lista_file_legacy,
        )

        # Sconfezionamento del codice generato in file fisici
        logger.info("Organizzazione del codice Backend in cartelle fisiche...")
        n_backend = unpack_markdown_to_files(str(cartella_output / FILE_BACKEND_IMPL), str(cartella_output))

        logger.info("Organizzazione del codice Frontend in cartelle fisiche...")
        n_frontend = unpack_markdown_to_files(str(cartella_output / FILE_FRONTEND_IMPL), str(cartella_output))

        shutil.make_archive(str(WORKSPACE_DIR / f"{session_id}_finale"), "zip", str(cartella_output))

        return {
            "status": "success",
            "messaggio": "Fase 3 (Implementation) completata. Progetto pronto per il Testing & Deployment umano.",
            "session_id": session_id,
            "file_migrati": esiti["completati"],
            "file_falliti": esiti["falliti"],
            "file_saltati_da_checkpoint": esiti["saltati"],
            "file_sorgente_estratti": {"backend": n_backend, "frontend": n_frontend},
            "url_download_progetto": f"/api/v1/modernize/download/{session_id}/3",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Errore in Fase 3, sessione %s", session_id)
        raise HTTPException(status_code=500, detail=str(e))


# =====================================================================
# DOWNLOAD DINAMICO
# =====================================================================

@app.get("/api/v1/modernize/download/{session_id}/{fase}")
def scarica_file(
    session_id: str,
    fase: int,
    user_id: str = Depends(get_current_user_and_validate_license),
):
    session_id = _valida_session_id(session_id)
    _verifica_proprieta_sessione(session_id, user_id)

    mappa_nomi = {1: "fase1", 2: "fase2", 3: "finale"}
    if fase not in mappa_nomi:
        raise HTTPException(status_code=400, detail="Fase non valida. Scegli tra 1, 2 o 3.")

    zip_path = WORKSPACE_DIR / f"{session_id}_{mappa_nomi[fase]}.zip"
    if not zip_path.exists():
        raise HTTPException(status_code=404, detail="File non trovato. Elabora la fase corrispondente.")

    return FileResponse(
        path=str(zip_path),
        media_type="application/zip",
        filename=f"Modernizzazione_Sessione_{session_id}_{mappa_nomi[fase]}.zip",
    )


# =====================================================================
# UPLOAD FILE MODIFICATI (HITL, tra un checkpoint e l'altro)
# =====================================================================

@app.post("/api/v1/modernize/upload/{session_id}")
def carica_file_modificati(
    session_id: str,
    files: List[UploadFile] = File(...),
    user_id: str = Depends(get_current_user_and_validate_license),  # PRIMA MANCAVA: endpoint aperto a chiunque
):
    session_id = _valida_session_id(session_id)
    _verifica_proprieta_sessione(session_id, user_id)

    cartella_output = _cartella_sessione(session_id)
    if not cartella_output.exists():
        raise HTTPException(status_code=404, detail="Sessione non trovata.")

    file_sovrascritti = []

    for file in files:
        # Il filename arriva dal client: os.path.basename elimina eventuali
        # componenti di percorso ('../auth.py' avrebbe sovrascritto file
        # FUORI dalla cartella di sessione).
        nome_sicuro = os.path.basename(file.filename or "")

        if not nome_sicuro.lower().endswith((".md", ".sql")):
            raise HTTPException(
                status_code=400,
                detail=f"Il file '{file.filename}' non è di un tipo consentito (.md o .sql).",
            )

        percorso_file = cartella_output / nome_sicuro
        with open(percorso_file, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        file_sovrascritti.append(nome_sicuro)

    return {
        "status": "success",
        "messaggio": "File verificati e sovrascritti con successo.",
        "file_aggiornati": file_sovrascritti,
    }


# =====================================================================
# ENDPOINT ADMIN
# =====================================================================

@app.get("/api/v1/admin/users")
def admin_ottieni_tutti_gli_utenti(user_id: str = Depends(require_admin)):
    try:
        risposta = supabase.table("profiles").select("id", "email").execute()
        return risposta.data
    except Exception as e:
        logger.error("Errore recupero utenti: %s", e)
        raise HTTPException(status_code=500, detail="Errore nel recupero degli utenti.")


@app.get("/api/v1/admin/sessions")
def admin_ottieni_tutte_le_sessioni(user_id: str = Depends(require_admin)):
    try:
        risposta = (
            supabase.table("migration_sessions")
            .select("*")
            .order("updated_at", desc=True)
            .execute()
        )
        return risposta.data
    except Exception as e:
        logger.error("Errore admin recupero sessioni: %s", e)
        raise HTTPException(status_code=500, detail="Errore admin nel recupero sessioni.")


@app.delete("/api/v1/admin/sessions/{session_id}")
def admin_cancella_sessione(session_id: str, user_id: str = Depends(require_admin)):
    # La validazione qui è CRITICA: senza, un session_id come '../..'
    # farebbe puntare shutil.rmtree fuori dal workspace.
    session_id = _valida_session_id(session_id)

    # 1. Pulizia del filesystem (cartella di lavoro + ZIP)
    cartella_sessione = WORKSPACE_DIR / session_id
    try:
        if cartella_sessione.exists() and cartella_sessione.is_dir():
            shutil.rmtree(cartella_sessione)
            logger.info("Cartella fisica eliminata: %s", cartella_sessione)

        for suffisso in ["_fase1.zip", "_fase2.zip", "_finale.zip"]:
            zip_path = WORKSPACE_DIR / f"{session_id}{suffisso}"
            if zip_path.exists():
                zip_path.unlink()
                logger.info("Archivio ZIP rimosso: %s", zip_path)
    except Exception as e:
        # Errore filesystem loggato ma non bloccante, per non lasciare il DB disallineato
        logger.warning("Errore parziale rimozione file di %s: %s", session_id, e)

    # 2. Cancellazione record su Supabase
    try:
        supabase.table("migration_sessions").delete().eq("id", session_id).execute()
        return {
            "status": "success",
            "messaggio": f"Sessione {session_id} e tutti i file correlati rimossi definitivamente.",
        }
    except Exception as e:
        logger.error("Errore eliminazione sessione %s dal DB: %s", session_id, e)
        raise HTTPException(status_code=500, detail="Errore durante l'eliminazione dal database.")
