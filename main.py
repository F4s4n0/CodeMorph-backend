import logging
import os
import re
import unicodedata
import shutil
import zipfile
import storage
import interruzione
import json
from src.preparazione import analizza_sorgenti
from src.graph_builder import ESCLUDI_CARTELLE, ESTENSIONI_VALIDE, MAX_FILE_SIZE
from affiliazione import router as affiliazione_router
from statistiche import router as statistiche_router

from pathlib import Path
from typing import Optional, List
from datetime import datetime, timezone
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Form, File, UploadFile, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from contacts import router as contacts_router

from auth import _parse_expiry, get_current_user, get_current_user_and_validate_license, supabase
from payments import addebita_consumo_token, addebita_importo_token, verifica_credito_token
from payments import router as payments_router
from src.code_unpacker import unpack_markdown_to_files
from src.config import FILE_BACKEND_IMPL, FILE_FRONTEND_IMPL, FILE_SELEZIONE, WORKSPACE_DIR
from src.crew import run_understanding_phase, run_design_phase, run_implementation_phase
from src.graph_builder import (
    ESTENSIONI_VALIDE,
    extract_foxpro_dbf_schema,
    extract_foxpro_scx_code,
    process_directory_to_graph,
)
from src.live_log import log_message
from src.llm_config import get_llm
from src.token_tracker import TokenUsageTracker
from trial import router as trial_router

load_dotenv()
logging.basicConfig(level=logging.INFO)


class MascheraChiaviURL(logging.Filter):
    """
    httpx registra l'URL completo di ogni richiesta HTTP. Google passa la
    chiave API in query string (?key=...), quindi senza questo filtro la
    chiave finisce IN CHIARO nei log di Render, che sono consultabili e
    spesso vengono copiati altrove per chiedere aiuto.
    Anthropic non e' esposta allo stesso modo (chiave nell'header).
    """

    _CHIAVE = re.compile(
        r"([?&](?:key|api_key|apikey|access_token|token)=)[^&\s\"']+",
        re.IGNORECASE,
    )

    def filter(self, record):
        try:
            record.msg = self._CHIAVE.sub(r"\1***", str(record.msg))
            if record.args:
                record.args = tuple(
                    self._CHIAVE.sub(r"\1***", a) if isinstance(a, str) else a
                    for a in record.args
                )
        except Exception:
            pass          # il logging non deve mai far fallire una richiesta
        return True


# Sul logger "httpx", non sul root: i filtri NON si propagano ai logger figli,
# quindi applicarlo altrove non avrebbe alcun effetto.
def _applica_maschera_chiavi():
    """
    Applica il filtro agli HANDLER, non a un singolo logger.

    Un filtro su logging.getLogger("httpx") cattura solo cio' che passa da
    quel logger: se domani LiteLLM, urllib3 o un'altra libreria registrasse
    un URL con la chiave in query string, non verrebbe mascherata. Sugli
    handler passa invece TUTTO cio' che viene scritto.

    Va richiamata anche dopo l'avvio: uvicorn installa i propri handler
    quando importa l'app, quindi un aggancio fatto solo a import-time
    potrebbe non coprirli.
    """
    maschera = MascheraChiaviURL()
    for logger_nome in ("", "httpx", "uvicorn", "uvicorn.error", "uvicorn.access", "LiteLLM"):
        obiettivo = logging.getLogger(logger_nome)
        if not any(isinstance(f, MascheraChiaviURL) for f in obiettivo.filters):
            obiettivo.addFilter(maschera)
        for handler in obiettivo.handlers:
            if not any(isinstance(f, MascheraChiaviURL) for f in handler.filters):
                handler.addFilter(maschera)


_applica_maschera_chiavi()

logger = logging.getLogger(__name__)

# WORKSPACE_DIR è definita in src/config.py: è l'UNICA fonte di verità,
# condivisa con src/live_log.py così i log live vengono scritti e letti
# dalla stessa cartella.
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="Piattaforma Enterprise di Modernizzazione Universale",
    description=(
        "API suddivise in fasi con Checkpoint umani (HITL), Capability Registry, "
        "pagamenti (pass giornaliero via PayPal/Google Pay) e credito token a consumo."
    ),
    version="2.2.0",
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
    # Senza questo il browser NASCONDE l'header a JavaScript e il frontend non
    # puo' leggere il nome file scelto dal server: gli ZIP tornerebbero a
    # chiamarsi tutti "Modernizzazione_Sessione_<uuid>".
    expose_headers=["Content-Disposition"],
)

# Endpoint di pagamento e credito token (payments.py)
app.include_router(payments_router)

# Endpoint di trial
app.include_router(trial_router)

# EndPoint Contatti
app.include_router(contacts_router)

#Affiliazione
app.include_router(affiliazione_router)

#Statistiche
app.include_router(statistiche_router)

@app.on_event("startup")
def _riapplica_maschera_chiavi():
    """
    Uvicorn installa i propri handler DOPO l'import del modulo: senza questa
    seconda applicazione il filtro coprirebbe solo gli handler esistenti a
    import-time, e una chiave API potrebbe comparire in chiaro nei log.
    """
    _applica_maschera_chiavi()


@app.on_event("startup")
def _sblocca_sessioni_orfane():
    """
    Al riavvio del server i BackgroundTasks del processo precedente sono
    morti: le sessioni rimaste 'running' non arriveranno mai a 'completata'.

    Qui vengono chiuse e — punto importante — viene ADDEBITATO il consumo
    parziale salvato da crew.py durante l'elaborazione. Senza, i token già
    fatturati da Anthropic resterebbero a carico della piattaforma.

    L'addebito è idempotente: il parziale viene azzerato contestualmente,
    così un secondo riavvio non lo conta due volte.
    """
    try:
        r = (supabase.table("migration_sessions")
             .select("id,user_id,costo_parziale_eur,token_parziali,fase_in_corso")
             .eq("stato_esecuzione", "running").execute())
    except Exception as e:
        logger.error("Sblocco sessioni orfane: lettura fallita: %s", e)
        return

    for riga in (r.data or []):
        session_id = riga["id"]
        costo = float(riga.get("costo_parziale_eur") or 0)
        tokens = int(riga.get("token_parziali") or 0)

        # 1. Addebito del consumo maturato prima del riavvio
        if costo > 0 and riga.get("user_id"):
            try:
                addebita_importo_token(
                    riga["user_id"], costo, tokens=tokens, session_id=session_id,
                    descrizione="Consumo parziale: elaborazione interrotta da un riavvio del server",
                )
                logger.warning(
                    "Sessione orfana %s: addebitati %.4f EUR (%d token).",
                    session_id, costo, tokens,
                )
            except Exception as e:
                logger.error("Addebito parziale fallito per %s: %s", session_id, e)

        # 2. Chiusura della sessione e azzeramento del parziale (idempotenza)
        try:
            supabase.table("migration_sessions").update({
                "stato_esecuzione": "errore",
                "errore_messaggio": (
                    "Elaborazione interrotta da un riavvio del server. "
                    "I token consumati fino a quel momento sono stati addebitati: "
                    "puoi rilanciare la fase."
                ),
                "token_parziali": 0,
                "costo_parziale_eur": 0,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", session_id).execute()
            log_message(session_id, "❌ Elaborazione interrotta da un riavvio del server: rilancia la fase.")
        except Exception as e:
            logger.error("Chiusura sessione orfana %s fallita: %s", session_id, e)


# =====================================================================
# Modelli di input
# =====================================================================

class InputFase2(BaseModel):
    session_id: str
    linguaggio_target: str
    provider_llm: str = "anthropic"
    modello_llm: str = "claude-sonnet-5"
    quality_gate: bool = False

class InputFase3(BaseModel):
    session_id: str
    linguaggio_target: str
    provider_llm: str = "anthropic"
    modello_llm: str = "claude-sonnet-5"
    quality_gate: bool = False


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

def _imposta_stato_esecuzione(session_id, stato, fase=None, errore=None, risultato=None):
    """
    Aggiorna lo stato di avanzamento della sessione su Supabase.
    Best-effort: un errore qui non deve mai far fallire la pipeline.
    stato: 'running' | 'completata' | 'errore'
    """
    dati = {"stato_esecuzione": stato, "updated_at": datetime.now(timezone.utc).isoformat()}
    if fase is not None:
        dati["fase_in_corso"] = fase
    # Sempre valorizzati (anche a None) per ripulire l'esito precedente
    dati["errore_messaggio"] = errore
    dati["risultato"] = risultato
    try:
        supabase.table("migration_sessions").update(dati).eq("id", session_id).execute()
    except Exception as e:
        logger.error("Stato esecuzione non aggiornato per %s: %s", session_id, e)


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

# File interni/ridondanti da NON includere negli zip consegnati all'utente
FILE_ESCLUSI_DALLO_ZIP = {
    "live_logs.txt",                   # log live: serve al terminale del frontend, non al deliverable
    "solution_upload.zip",             # copia dello zip caricato dall'utente: ce l'ha già
    "_implementation_checkpoint.json", # stato interno del resume della Fase 3
    "_segreti.json",                   # credenziali rilevate nei sorgenti: MAI consegnarle
    # _file_selezionati.json NON e' piu' escluso: serve al cliente come prova
    # di cosa ha scelto, e soprattutto deve sopravvivere a un riavvio di
    # Render. Essendo escluso dallo zip spariva dal backup, e la Fase 3 —
    # ripartendo da una sessione ripristinata — non lo trovava piu' e migrava
    # TUTTI i file invece dei soli selezionati.
}

def _scrivi_elenco_selezione(cartella_output, ammessi, nome_progetto=""):
    """
    Documento leggibile con i file scelti per l'analisi.

    Il cliente sceglie in una schermata e poi non ne ha piu' traccia: se un
    modulo non compare nei documenti finali, non ha modo di sapere se e' stato
    escluso da lui o ignorato dal sistema. Qui trova l'elenco esatto, raggruppato
    per tipo, con i totali.
    """
    per_estensione = {}
    for percorso in sorted(ammessi):
        estensione = (os.path.splitext(percorso)[1] or "(senza estensione)").lower()
        per_estensione.setdefault(estensione, []).append(percorso)

    righe = [
        "# File selezionati per l'analisi",
        "",
        f"**Progetto:** {nome_progetto or 'senza nome'}  ",
        f"**Data:** {datetime.now(timezone.utc).strftime('%d/%m/%Y')}  ",
        f"**Totale file analizzati:** {len(ammessi)}",
        "",
        "Questo e' l'elenco esatto dei file su cui hanno lavorato gli agenti.",
        "I file non presenti erano stati esclusi in fase di selezione e non",
        "compaiono nei documenti prodotti.",
        "",
        "## Riepilogo per tipologia",
        "",
        "| Tipo | N. file |",
        "|---|---|",
    ]
    for estensione, elenco in sorted(per_estensione.items(), key=lambda x: -len(x[1])):
        righe.append(f"| `{estensione}` | {len(elenco)} |")

    righe.append("")
    righe.append("## Elenco completo")
    for estensione, elenco in sorted(per_estensione.items(), key=lambda x: -len(x[1])):
        righe.append("")
        righe.append(f"### `{estensione}` — {len(elenco)} file")
        righe.append("")
        for percorso in elenco:
            righe.append(f"- `{percorso}`")

    with open(os.path.join(str(cartella_output), "0_File_Selezionati.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(righe) + "\n")


def _crea_zip_fase(percorso_base_senza_estensione, cartella_sessione, escludi_cartelle=()):
    """
    Crea lo zip di consegna di una fase escludendo i file interni
    (FILE_ESCLUSI_DALLO_ZIP) e, opzionalmente, intere cartelle
    (es. 'sorgenti_originali' negli zip dove non serve).
    Sostituisce shutil.make_archive, che non supporta esclusioni.
    """
    percorso_zip = f"{percorso_base_senza_estensione}.zip"
    with zipfile.ZipFile(percorso_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(cartella_sessione):
            # Potatura delle cartelle escluse: modificare dirs "sul posto"
            # impedisce a os.walk di scendere dentro di esse
            dirs[:] = [d for d in dirs if d not in escludi_cartelle]
            for nome in files:
                if nome in FILE_ESCLUSI_DALLO_ZIP:
                    continue
                completo = os.path.join(root, nome)
                zf.write(completo, os.path.relpath(completo, cartella_sessione))
    return percorso_zip

# =====================================================================
# Credito token (quota inclusa nel pass + ricariche)
# =====================================================================

def _chiudi_conteggio_token(user_id: str, tracker, session_id: str):
    """
    Addebita a fine fase il costo REALE dei token consumati e lo scrive nel
    log live. Ritorna il blocco 'token' da includere nella risposta JSON
    (None se la fase non ha consumato nulla).
    """
    if tracker is None or (tracker.tokens_totali == 0 and tracker.richieste == 0):
        return None

    costo, saldo = addebita_consumo_token(user_id, tracker, session_id=session_id)
    if saldo is not None:
        log_message(
            session_id,
            f"🪙 Token consumati nella fase: {tracker.tokens_totali} "
            f"(≈ {costo:.4f} €). Credito residuo: {saldo:.2f} €.",
        )
        if saldo <= 0:
            log_message(
                session_id,
                "⚠️ Credito token esaurito: ricarica o acquista un nuovo pass per le prossime fasi.",
            )
    else:
        log_message(
            session_id,
            f"🪙 Token consumati nella fase: {tracker.tokens_totali} (≈ {costo:.4f} €).",
        )

    return {
        "tokens_totali": tracker.tokens_totali,
        "tokens_prompt": tracker.prompt_tokens,
        "tokens_completion": tracker.completion_tokens,
        "costo_eur": float(costo),
        "saldo_residuo_eur": float(saldo) if saldo is not None else None,
    }


def require_admin(user_id: str = Depends(get_current_user)):
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
# ENDPOINT DI PREPARAZIONE SORGENTI (ZIP)
# =====================================================================

@app.post("/api/v1/modernize/prepara/{session_id}")
def prepara_sorgenti(
    session_id: str,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    # Provider e modello scelti dall'utente: la pre-selezione deve girare
    # sul SUO modello, non su uno fisso. I default coprono i client datati
    # che non li inviano ancora.
    provider_llm: str = Form(""),
    modello_llm: str = Form(""),
    # Il nome serve gia' qui: la sessione viene registrata prima dell'analisi
    # e senza di esso comparirebbe senza nome in "I Miei Progetti".
    session_name: str = Form(""),
    user_id: str = Depends(get_current_user_and_validate_license),
):
    """
    Estrae lo ZIP e restituisce l'elenco dei file candidati all'analisi,
    con i rilevanti già selezionati. Non avvia nulla: l'utente conferma.
    """
    session_id = _valida_session_id(session_id)
    _verifica_proprieta_sessione(session_id, user_id)
    verifica_credito_token(user_id)

    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Carica un archivio .zip")

    cartella_output = _cartella_sessione(session_id)
    cartella_output.mkdir(parents=True, exist_ok=True)
    cartella_sorgenti = cartella_output / "sorgenti_originali"
    cartella_sorgenti.mkdir(exist_ok=True)

    zip_path = cartella_output / "solution_upload.zip"
    with open(zip_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    try:
        _estrai_zip_sicuro(zip_path, cartella_sorgenti)
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Il file non è un archivio ZIP valido o è corrotto.")

    # Backup dedicato dei sorgenti, UNA volta sola: non cambiano piu' e non
    # devono finire negli zip di fase, che il cliente scarica.
    #
    # In BACKGROUND: comprimere e caricare decine di MB richiede minuti, e
    # farlo prima di rispondere significa lasciare il cliente davanti a uno
    # spinner per tutto il tempo. L'elenco dei file non dipende dal backup,
    # quindi puo' partire subito; il backup serve solo a un eventuale
    # ripristino DOPO un riavvio, che nel frattempo non e' ancora possibile.
    background.add_task(storage.salva_sorgenti, session_id, str(cartella_output))

    # La sessione va registrata ORA, non all'avvio della Fase 1: il consumo
    # della pre-selezione viene addebitato qui, e il movimento su
    # token_transactions fa riferimento a questa sessione. E' un upsert, e
    # l'avvio della fase la sovrascrive con nome e impostazioni definitive.
    try:
        supabase.table("migration_sessions").upsert({
            "id": session_id,
            "user_id": user_id,
            "session_name": session_name or "Progetto senza nome",
            "current_step": "input",
            "provider_llm": provider_llm or None,
            "modello_llm": modello_llm or None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        # Non blocca: l'analisi puo' proseguire, al massimo il movimento
        # token non verra' collegato alla sessione.
        logger.warning("Registrazione anticipata della sessione %s fallita: %s", session_id, e)

    # La classificazione dei file e' una chiamata LLM come le altre: va
    # contabilizzata, altrimenti su archivi grandi il costo resta a carico
    # della piattaforma.
    tracker = TokenUsageTracker(modello_llm)
    elenco = analizza_sorgenti(
        str(cartella_sorgenti), ESCLUDI_CARTELLE, ESTENSIONI_VALIDE, MAX_FILE_SIZE,
        provider=provider_llm or None, modello=modello_llm or None, tracker=tracker,
    )
    _chiudi_conteggio_token(user_id, tracker, session_id)

    if not elenco:
        raise HTTPException(
            status_code=400,
            detail="Nessun file di codice riconosciuto nell'archivio.",
        )
    return {"file": elenco, "totale": len(elenco)}

# =====================================================================
# ENDPOINT DI STATO SESSIONE
# =====================================================================


@app.get("/api/v1/modernize/stato/{session_id}")
def stato_esecuzione(session_id: str, user_id: str = Depends(get_current_user)):
    """
    Avanzamento della sessione: il frontend lo interroga in polling mentre
    la fase gira in background.

    Se l'elaborazione risulta ferma da oltre un'ora, il campo
    `possibile_blocco` lo segnala al frontend. Nessuna decisione automatica:
    su progetti grandi un'attesa lunga è legittima, quindi è il cliente a
    scegliere se attendere ancora o chiudere l'elaborazione.
    """
    session_id = _valida_session_id(session_id)
    _verifica_proprieta_sessione(session_id, user_id)

    try:
        r = (supabase.table("migration_sessions")
             .select("stato_esecuzione,fase_in_corso,errore_messaggio,risultato,current_step,updated_at")
             .eq("id", session_id).execute())
    except Exception as e:
        logger.error("Lettura stato esecuzione fallita per %s: %s", session_id, e)
        raise HTTPException(status_code=503, detail="Servizio non disponibile.")

    if not r.data:
        raise HTTPException(status_code=404, detail="Sessione non trovata.")

    dati = r.data[0]
    dati["possibile_blocco"] = False
    dati["fermo_da_minuti"] = None

    if dati.get("stato_esecuzione") == "running":
        try:
            aggiornato = datetime.fromisoformat(dati["updated_at"].replace("Z", "+00:00"))
            if aggiornato:
                minuti = int((datetime.now(timezone.utc) - aggiornato).total_seconds() // 60)
                dati["fermo_da_minuti"] = minuti
                dati["possibile_blocco"] = minuti >= 20
        except Exception:
            # Il calcolo è un di più: se il formato della data non si legge,
            # lo stato viene restituito comunque senza segnalazione.
            pass

    return dati

# =====================================================================
# ENDPOINT DI STOP SESSIONE
# =====================================================================


@app.post("/api/v1/modernize/stop/{session_id}")
def interrompi_fase(session_id: str, user_id: str = Depends(get_current_user)):
    """
    Chiede l'interruzione della fase in corso. Lo stop è cooperativo:
    il lavoro si ferma al primo punto di controllo utile.
    """
    session_id = _valida_session_id(session_id)
    _verifica_proprieta_sessione(session_id, user_id)

    interruzione.richiedi_stop(session_id)
    log_message(session_id, "🛑 Interruzione richiesta: la fase si fermerà tra pochi istanti...")
    return {"status": "stop_richiesto"}

# =====================================================================
# FASE 1: UNDERSTANDING (doppia modalità ZIP / testo)
# =====================================================================

def _lavoro_fase1(session_id, user_id, provider_llm, modello_llm,
                  ha_file, codice_legacy, quality_gate,file_ammessi=None, gia_estratto=False):
    """
    Elaborazione della Fase 1 in background: tutto ciò che prima stava
    dentro il try dell'endpoint. Non solleva mai: registra l'esito su DB.
    """
    tracker = TokenUsageTracker(modello_llm)
    cartella_output = _cartella_sessione(session_id)
    cartella_sorgenti = cartella_output / "sorgenti_originali"

    try:
        
        _imposta_stato_esecuzione(session_id, "running", fase="fase1")
        llm = get_llm(provider=provider_llm, model_name=modello_llm)

        if ha_file or gia_estratto:
            if gia_estratto:
                # I sorgenti sono già stati estratti dall'endpoint /prepara:
                # ri-estrarre sovrascriverebbe inutilmente i file.
                log_message(session_id, "📂 Sorgenti già estratti: avvio analisi delle dipendenze...")
            else:
                log_message(session_id, "🗂️ Estrazione dell'archivio e analisi delle dipendenze...")
                zip_path = cartella_output / "solution_upload.zip"
                try:
                    _estrai_zip_sicuro(zip_path, cartella_sorgenti)
                except zipfile.BadZipFile:
                    raise ValueError("Il file non è un archivio ZIP valido o è corrotto.")

            if file_ammessi:
                log_message(session_id, f"🎯 Analisi limitata ai {len(file_ammessi)} file selezionati.")

            codice_da_analizzare = process_directory_to_graph(
                cartella_sorgenti, llm, session_id, tracker=tracker,
                file_ammessi=set(file_ammessi) if file_ammessi else None,
            )
        else:
            log_message(session_id, "📝 Analisi dello script di testo singolo avviata...")
            codice_da_analizzare = codice_legacy

        log_message(session_id, "🤖 Avvio del Team AI per la stesura della documentazione tecnica formale...")
        run_understanding_phase(
            llm=llm,
            codice_legacy=codice_da_analizzare,
            output_dir=str(cartella_output),
            session_id=session_id,
            tracker=tracker,
            quality_gate=quality_gate,
        )

        log_message(session_id, "🗜️ Generazione del pacchetto ZIP del codice e dei report in corso...")
        # I sorgenti restano fuori: sono decine di MB che il cliente ha gia'
        # caricato lui, e hanno un backup dedicato su Storage per la Fase 3.
        percorso_zip = _crea_zip_fase(
            str(WORKSPACE_DIR / f"{session_id}_fase1"), str(cartella_output),
            escludi_cartelle=("sorgenti_originali",),
        )
        storage.salva_zip_fase(session_id, "fase1", percorso_zip)

        blocco_token = _chiudi_conteggio_token(user_id, tracker, session_id)
        log_message(session_id, "✨ [SUCCESS]: Fase 1 completata. Report pronti per l'ispezione umana.")

        # La Fase 1 è finita: la sessione è ora al Checkpoint 1.
        # Senza questo aggiornamento il DB resta a "input" e ricaricando
        # il progetto l'utente ripartirebbe dall'inizio.
        try:
            supabase.table("migration_sessions").upsert({
                "id": session_id,
                "user_id": user_id,
                "current_step": "cp1",
            }).execute()
        except Exception as e:
            logger.error("Aggiornamento current_step fallito per %s: %s", session_id, e)
            
        _imposta_stato_esecuzione(
            session_id, "completata", fase="fase1",
            risultato={"token": blocco_token,
                       "url_download": f"/api/v1/modernize/download/{session_id}/1"},
        )
    except interruzione.FaseInterrotta:
        # I token già consumati vanno addebitati: sono stati spesi davvero
        _chiudi_conteggio_token(user_id, tracker, session_id)
        logger.info("Fase 1 interrotta dall'utente, sessione %s", session_id)
        log_message(session_id, "🛑 Elaborazione interrotta. I token consumati fino a questo punto sono stati addebitati.")
        _imposta_stato_esecuzione(session_id, "interrotta", fase="fase1",
                                  errore="Interrotta su richiesta dell'utente.") 
    except Exception as e:
        # I token già consumati prima del crash vanno comunque contabilizzati
        _chiudi_conteggio_token(user_id, tracker, session_id)
        logger.exception("Errore in Fase 1, sessione %s", session_id)
        log_message(session_id, f"❌ ERRORE CRITICO DI SISTEMA: {e}")
        _imposta_stato_esecuzione(session_id, "errore", fase="fase1", errore=str(e))
    finally:
        interruzione.pulisci(session_id)

@app.post("/api/v1/modernize/understand", status_code=202)
def fase1_understand(
    background_tasks: BackgroundTasks,
    provider_llm: str = Form(...),
    modello_llm: str = Form(...),
    session_id: str = Form(...),
    session_name: str = Form("Progetto Senza Nome"),
    file: Optional[UploadFile] = File(None),
    codice_legacy: Optional[str] = Form(None),
    user_id: str = Depends(get_current_user_and_validate_license),
    quality_gate: bool = Form(False),
    file_selezionati: Optional[str] = Form(None),   # JSON: ["path/a.cs", ...]
):
    session_id = _valida_session_id(session_id)
    _verifica_proprieta_sessione(session_id, user_id)

    # Credito token: blocca subito (402) chi ha esaurito la quota
    verifica_credito_token(user_id)

    cartella_output = _cartella_sessione(session_id)
    cartella_output.mkdir(parents=True, exist_ok=True)
    cartella_sorgenti = cartella_output / "sorgenti_originali"
    cartella_sorgenti.mkdir(exist_ok=True)

    # Tre modalità valide: ZIP appena caricato, sorgenti già estratti da
    # /prepara (il client manda solo la selezione), oppure codice incollato.
    gia_estratto = any(cartella_sorgenti.iterdir())
    if not file and not gia_estratto and not (codice_legacy and codice_legacy.strip()):
        raise HTTPException(
            status_code=400,
            detail="Fornisci un archivio .zip della Solution oppure il codice legacy come testo.",
        )

    ammessi = None
    if file_selezionati:
        try:
            ammessi = json.loads(file_selezionati)
            if not isinstance(ammessi, list) or not ammessi:
                raise ValueError
        except Exception:
            raise HTTPException(status_code=400, detail="Elenco dei file selezionati non valido.")

    # Persisti la selezione: serve anche alla Fase 3, che gira giorni dopo
    if ammessi:
        try:
            with open(cartella_output / FILE_SELEZIONE, "w", encoding="utf-8") as f:
                json.dump(ammessi, f)
        except Exception as e:
            logger.warning("Selezione file non salvata per %s: %s", session_id, e)

        # Versione leggibile per il cliente: un JSON con centinaia di percorsi
        # e' una prova, non un documento. Cosi' puo' verificare in un colpo
        # d'occhio che l'analisi abbia coperto quello che si aspettava.
        try:
            _scrivi_elenco_selezione(cartella_output, ammessi, session_name)
        except Exception as e:
            logger.warning("Elenco file selezionati non scritto per %s: %s", session_id, e)

    # IMPORTANTE: il file caricato va salvato ORA. Dopo la risposta HTTP
    # lo stream di UploadFile viene chiuso e il background non lo vedrebbe più.
    ha_file = file is not None
    if ha_file:
        zip_path = cartella_output / "solution_upload.zip"
        with open(zip_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

    # Registrazione della sessione
    try:
        supabase.table("migration_sessions").upsert({
            "id": session_id,
            "user_id": user_id,
            "session_name": session_name,
            "current_step": "input",
            "quality_gate": quality_gate,
            "provider_llm": provider_llm,
            "modello_llm": modello_llm,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        logger.error("Registrazione sessione fallita: %s", e)


    _imposta_stato_esecuzione(session_id, "running", fase="fase1")
    log_message(session_id, "⚡ Ricezione completata: elaborazione avviata sul server.")

    background_tasks.add_task(
        _lavoro_fase1, session_id, user_id, provider_llm, modello_llm,
        ha_file, codice_legacy, quality_gate, ammessi, gia_estratto,
    )
    return {"status": "avviata", "session_id": session_id}

# =====================================================================
# LOG LIVE
# =====================================================================

def _formatta_riga_log(linea: str) -> str:
    """Colora la riga in base al tipo di evento (le emoji sono i marcatori)."""
    if "❌" in linea:
        return f"<span style='color: #ef4444;'>&gt; {linea}</span>"
    if "⚠️" in linea or "🪙" in linea:
        return f"<span style='color: #f59e0b;'>&gt; {linea}</span>"
    if "📈" in linea or "✅" in linea or "✨" in linea:
        return f"<span style='color: #22c55e;'>&gt; {linea}</span>"
    if "📦" in linea or "🗄️" in linea:
        return f"<span style='color: #a855f7;'>&gt;</span> {linea}"
    if "🧠" in linea or "🏛️" in linea or "⚙️" in linea or "🕵️" in linea:
        return f"<span style='color: #3b82f6;'>&gt;</span> {linea}"
    return f"&gt; {linea}"


@app.get("/api/v1/modernize/logs/{session_id}")
def ottieni_log_live(
    session_id: str,
    da_riga: int = 0,
    # Solo autenticazione: NON la verifica licenza. Questo endpoint legge un
    # file gia' prodotto e viene interrogato in continuo durante le fasi; se
    # la verifica licenza fallisce per un problema temporaneo di risorse
    # ([Errno 11] sotto carico LLM), il log si congela in interfaccia mentre
    # gli agenti stanno lavorando. La proprieta' della sessione, che e' il
    # controllo che conta davvero, viene verificata due righe piu' sotto.
    user_id: str = Depends(get_current_user),
):
    """
    Log live della sessione, sincronizzati con l'attività REALE degli agenti
    (ogni riga è scritta dai callback delle Crew, con timestamp).

    `da_riga` permette il polling incrementale: il frontend memorizza
    `righe_totali` dell'ultima risposta e richiede solo le righe nuove,
    invece di ricostruire (o simulare a tempo) l'intero log.
    """
    session_id = _valida_session_id(session_id)
    _verifica_proprieta_sessione(session_id, user_id)

    log_path = WORKSPACE_DIR / session_id / "live_logs.txt"

    if not log_path.exists():
        return {"logs": "Inizializzazione sessione di log...", "righe_totali": 0, "da_riga": 0}

    with open(log_path, "r", encoding="utf-8") as f:
        contenuto = f.read()

    righe = contenuto.splitlines()
    totale = len(righe)
    da_riga = max(0, min(da_riga, totale))

    linee_formattate = [
        _formatta_riga_log(linea) for linea in righe[da_riga:] if linea.strip()
    ]
    return {
        "logs": "<br>".join(linee_formattate),
        "righe_totali": totale,
        "da_riga": da_riga,
    }


# =====================================================================
# FASE 2: DESIGN (dopo il Checkpoint 1)
# =====================================================================

def _lavoro_fase2(session_id, user_id, provider_llm, modello_llm,
                  linguaggio_target, quality_gate):
    """Elaborazione della Fase 2 in background. Non solleva: registra l'esito su DB."""
    tracker = TokenUsageTracker(modello_llm)
    cartella_output = _cartella_sessione(session_id)

    try:
        _imposta_stato_esecuzione(session_id, "running", fase="fase2")
        log_message(
            session_id,
            f"🏛️ Avvio Fase 2 (Design): architettura e piano di migrazione verso {linguaggio_target}...",
        )

        llm = get_llm(provider=provider_llm, model_name=modello_llm)
        run_design_phase(
            llm=llm,
            linguaggio_target=linguaggio_target,
            output_dir=str(cartella_output),
            session_id=session_id,
            tracker=tracker,
            quality_gate=quality_gate,
        )

        percorso_zip = _crea_zip_fase(
            str(WORKSPACE_DIR / f"{session_id}_fase2"), str(cartella_output),
            escludi_cartelle=("sorgenti_originali",),
        )
        storage.salva_zip_fase(session_id, "fase2", percorso_zip)

        blocco_token = _chiudi_conteggio_token(user_id, tracker, session_id)
        log_message(session_id, "✨ [SUCCESS]: Fase 2 completata. Migration Plan e Schema DB pronti per il Checkpoint 2 umano.")

        # current_step avanza SOLO ora che il lavoro è davvero finito
        try:
            supabase.table("migration_sessions").upsert({
                "id": session_id,
                "user_id": user_id,
                "current_step": "cp2",
                "linguaggio_target": linguaggio_target,
            }).execute()
        except Exception as e:
            logger.error("Aggiornamento current_step fallito per %s: %s", session_id, e)

        _imposta_stato_esecuzione(
            session_id, "completata", fase="fase2",
            risultato={"token": blocco_token,
                       "url_download": f"/api/v1/modernize/download/{session_id}/2"},
        )
    except interruzione.FaseInterrotta:
        # I token già consumati vanno addebitati: sono stati spesi davvero
        _chiudi_conteggio_token(user_id, tracker, session_id)
        logger.info("Fase 1 interrotta dall'utente, sessione %s", session_id)
        log_message(session_id, "🛑 Elaborazione interrotta. I token consumati fino a questo punto sono stati addebitati.")
        _imposta_stato_esecuzione(session_id, "interrotta", fase="fase1",
                                  errore="Interrotta su richiesta dell'utente.")
    except Exception as e:
        _chiudi_conteggio_token(user_id, tracker, session_id)
        logger.exception("Errore in Fase 2, sessione %s", session_id)
        log_message(session_id, f"❌ ERRORE CRITICO IN FASE 2: {e}")
        _imposta_stato_esecuzione(session_id, "errore", fase="fase2", errore=str(e))
    finally:
        interruzione.pulisci(session_id)


@app.post("/api/v1/modernize/design", status_code=202)
def fase2_design(
    background_tasks: BackgroundTasks,
    richiesta: InputFase2,
    user_id: str = Depends(get_current_user_and_validate_license),
):
    session_id = _valida_session_id(richiesta.session_id)
    _verifica_proprieta_sessione(session_id, user_id)

    cartella_output = _cartella_sessione(session_id)
    if not cartella_output.exists() or not any(cartella_output.iterdir()):
        # Dopo un deploy/riavvio il disco è vuoto: prova il ripristino dai backup
        ripristinate = storage.ripristina_sessione(session_id, str(cartella_output))
        if not ripristinate:
            raise HTTPException(status_code=404, detail="Sessione non trovata. Elabora prima la Fase 1.")
        logger.info("Sessione %s ripristinata da Storage: %s", session_id, ripristinate)

    saldo_token = verifica_credito_token(user_id)
    if saldo_token is not None:
        log_message(session_id, f"🪙 Credito token disponibile: {saldo_token:.2f} €.")

    _imposta_stato_esecuzione(session_id, "running", fase="fase2")
    background_tasks.add_task(
        _lavoro_fase2, session_id, user_id,
        richiesta.provider_llm, richiesta.modello_llm,
        richiesta.linguaggio_target, richiesta.quality_gate,
    )
    return {"status": "avviata", "session_id": session_id}

# =====================================================================
# FASE 3: IMPLEMENTATION (dopo il Checkpoint 2)
# =====================================================================

def _carica_file_legacy(cartella_sorgenti: Path,file_ammessi=None):
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
            # Rispetta la selezione dell'utente: il percorso deve essere
            # calcolato come in Fase 1 (relativo a sorgenti_originali, slash
            # normalizzati), altrimenti il confronto non combacia mai.
            relativo = os.path.relpath(file_path, cartella_sorgenti).replace("\\", "/")
            if file_ammessi is not None and relativo not in file_ammessi:
                continue
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


def _lavoro_fase3(session_id, user_id, provider_llm, modello_llm,
                  linguaggio_target, quality_gate):
    """Elaborazione della Fase 3 in background. Non solleva: registra l'esito su DB."""
    tracker = TokenUsageTracker(modello_llm)
    cartella_output = _cartella_sessione(session_id)
    cartella_sorgenti = cartella_output / "sorgenti_originali"

    try:
        _imposta_stato_esecuzione(session_id, "running", fase="fase3")
        log_message(
            session_id,
            f"⚙️ Avvio Fase 3 (Implementation): generazione del codice {linguaggio_target}...",
        )

        llm = get_llm(provider=provider_llm, model_name=modello_llm)

        # Rispetta la selezione fatta dall'utente in Fase 1: i file che aveva
        # escluso non vanno migrati (e non vanno pagati).
        ammessi_fase3 = None
        percorso_sel = cartella_output / FILE_SELEZIONE
        if percorso_sel.exists():
            try:
                with open(percorso_sel, "r", encoding="utf-8") as f:
                    ammessi_fase3 = set(json.load(f))
                log_message(session_id, f"🎯 Migrazione limitata ai {len(ammessi_fase3)} file selezionati in Fase 1.")
            except Exception as e:
                logger.warning("Selezione file non leggibile per %s: %s", session_id, e)

        lista_file_legacy = _carica_file_legacy(cartella_sorgenti, file_ammessi=ammessi_fase3)
        if not lista_file_legacy:
            lista_file_legacy = [{"nome": "pasted_code.txt", "codice": "Nessun codice trovato."}]

        esiti = run_implementation_phase(
            llm=llm,
            linguaggio_target=linguaggio_target,
            output_dir=str(cartella_output),
            lista_file_legacy_estratti=lista_file_legacy,
            session_id=session_id,
            tracker=tracker,
            quality_gate=quality_gate,
        )

        log_message(session_id, "🗜️ Sconfezionamento del codice generato in file fisici e ZIP finale...")
        n_backend = unpack_markdown_to_files(str(cartella_output / FILE_BACKEND_IMPL), str(cartella_output))
        n_frontend = unpack_markdown_to_files(str(cartella_output / FILE_FRONTEND_IMPL), str(cartella_output))

        percorso_zip = _crea_zip_fase(
            str(WORKSPACE_DIR / f"{session_id}_finale"), str(cartella_output),
            escludi_cartelle=("sorgenti_originali",),
        )
        storage.salva_zip_fase(session_id, "finale", percorso_zip)

        blocco_token = _chiudi_conteggio_token(user_id, tracker, session_id)
        log_message(session_id, "✨ [SUCCESS]: Fase 3 completata. Progetto pronto per il Testing & Deployment umano.")

        try:
            supabase.table("migration_sessions").upsert({
                "id": session_id,
                "user_id": user_id,
                "current_step": "final",
                "linguaggio_target": linguaggio_target,
            }).execute()
        except Exception as e:
            logger.error("Aggiornamento current_step fallito per %s: %s", session_id, e)

        _imposta_stato_esecuzione(
            session_id, "completata", fase="fase3",
            risultato={
                "token": blocco_token,
                "file_migrati": esiti["completati"],
                "file_falliti": esiti["falliti"],
                "file_saltati_da_checkpoint": esiti["saltati"],
                "file_sorgente_estratti": {"backend": n_backend, "frontend": n_frontend},
                "url_download": f"/api/v1/modernize/download/{session_id}/3",
            },
        )
    except interruzione.FaseInterrotta:
        # I token già consumati vanno addebitati: sono stati spesi davvero
        _chiudi_conteggio_token(user_id, tracker, session_id)
        logger.info("Fase 1 interrotta dall'utente, sessione %s", session_id)
        log_message(session_id, "🛑 Elaborazione interrotta. I token consumati fino a questo punto sono stati addebitati.")
        _imposta_stato_esecuzione(session_id, "interrotta", fase="fase1",
                                  errore="Interrotta su richiesta dell'utente.")
    except Exception as e:
        _chiudi_conteggio_token(user_id, tracker, session_id)
        logger.exception("Errore in Fase 3, sessione %s", session_id)
        log_message(session_id, f"❌ ERRORE CRITICO IN FASE 3: {e}")
        _imposta_stato_esecuzione(session_id, "errore", fase="fase3", errore=str(e))
    finally:
        interruzione.pulisci(session_id)


@app.post("/api/v1/modernize/implement", status_code=202)
def fase3_implement(
    background_tasks: BackgroundTasks,
    richiesta: InputFase3,
    user_id: str = Depends(get_current_user_and_validate_license),
):
    session_id = _valida_session_id(richiesta.session_id)
    _verifica_proprieta_sessione(session_id, user_id)

    cartella_output = _cartella_sessione(session_id)
    if not cartella_output.exists() or not any(cartella_output.iterdir()):
        # Dopo un deploy/riavvio il disco è vuoto: prova il ripristino dai backup
        ripristinate = storage.ripristina_sessione(session_id, str(cartella_output))
        if not ripristinate:
            raise HTTPException(status_code=404, detail="Sessione non trovata. Elabora prima la Fase 1.")
        logger.info("Sessione %s ripristinata da Storage: %s", session_id, ripristinate)

    saldo_token = verifica_credito_token(user_id)
    if saldo_token is not None:
        log_message(session_id, f"🪙 Credito token disponibile: {saldo_token:.2f} €.")

    _imposta_stato_esecuzione(session_id, "running", fase="fase3")
    background_tasks.add_task(
        _lavoro_fase3, session_id, user_id,
        richiesta.provider_llm, richiesta.modello_llm,
        richiesta.linguaggio_target, richiesta.quality_gate,
    )
    return {"status": "avviata", "session_id": session_id}

# =====================================================================
# DOWNLOAD DINAMICO
# =====================================================================

def _nome_file_zip(session_id, etichetta_fase):
    """
    Nome dello ZIP consegnato al cliente, a partire dal nome che ha dato lui
    al progetto. Un archivio chiamato "Modernizzazione_Sessione_b0e25c52-...zip"
    e' inservibile nella cartella Download: con dieci progetti non si distingue
    piu' quale sia quale.

    L'UUID resta in coda, abbreviato: serve a non sovrascrivere due esportazioni
    con lo stesso nome e a ritrovare la sessione in caso di assistenza.
    """
    nome = ""
    try:
        r = (supabase.table("migration_sessions").select("session_name")
             .eq("id", session_id).limit(1).execute())
        if r.data:
            nome = (r.data[0].get("session_name") or "").strip()
    except Exception as e:
        logger.warning("Nome progetto non leggibile per %s (%s): uso il nome generico.",
                       session_id, type(e).__name__)

    # Accenti e caratteri non ASCII fuori: alcuni browser e file system li
    # gestiscono male in Content-Disposition.
    nome = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode()
    nome = re.sub(r"[^A-Za-z0-9._-]+", "_", nome).strip("_")[:60]
    if not nome:
        nome = "Modernizzazione"
    return f"{nome}_{etichetta_fase}_{session_id[:8]}.zip"


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
        # Zip locale perso (deploy): prova a recuperarlo dal backup
        if not storage.scarica_zip_fase(session_id, mappa_nomi[fase], str(zip_path)):
            raise HTTPException(status_code=404, detail="File non trovato. Elabora la fase corrispondente.")
    
    return FileResponse(
        path=str(zip_path),
        media_type="application/zip",
        filename=_nome_file_zip(session_id, mappa_nomi[fase]),
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
def admin_ottieni_tutte_le_sessioni(user_id: str = Depends(get_current_user)):
    try:
        utente = supabase.table("profiles").select("role").eq("id", user_id).single().execute()
        is_admin = utente.data and utente.data.get("role") == "admin"

        if is_admin:
            # L'admin vede tutto il database
            risposta = supabase.table("migration_sessions").select("*").order("updated_at", desc=True).execute()
        else:
            # L'utente vede solo la sua roba
            risposta = supabase.table("migration_sessions").select("*").eq("user_id", user_id).order("updated_at", desc=True).execute()
            
        return risposta.data
    except Exception as e:
        logger.error("Errore recupero sessioni: %s", e)
        raise HTTPException(status_code=500, detail="Errore nel recupero sessioni.")


@app.delete("/api/v1/admin/sessions/{session_id}")
def admin_cancella_sessione(session_id: str, user_id: str = Depends(get_current_user)):
    session_id = _valida_session_id(session_id)

    # 1. Controllo permessi
    try:
        utente = supabase.table("profiles").select("role").eq("id", user_id).single().execute()
        is_admin = utente.data and utente.data.get("role") == "admin"

        if not is_admin:
            # Se non è admin, usa la funzione già esistente che blocca con 403 se l'ID non è suo
            _verifica_proprieta_sessione(session_id, user_id)
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Errore verifica permessi per cancellazione: %s", e)
        raise HTTPException(status_code=500, detail="Errore durante la verifica dei permessi.")

    # 2. Pulizia del filesystem (cartella di lavoro + ZIP)
    cartella_sessione = WORKSPACE_DIR / session_id
    try:
        if cartella_sessione.exists() and cartella_sessione.is_dir():
            shutil.rmtree(cartella_sessione)
            logger.info("Cartella fisica eliminata: %s", cartella_sessione)
            storage.elimina_backup_sessione(session_id)
        for suffisso in ["_fase1.zip", "_fase2.zip", "_finale.zip"]:
            zip_path = WORKSPACE_DIR / f"{session_id}{suffisso}"
            if zip_path.exists():
                zip_path.unlink()
                logger.info("Archivio ZIP rimosso: %s", zip_path)
    except Exception as e:
        logger.warning("Errore parziale rimozione file di %s: %s", session_id, e)

    # 3. Cancellazione record su Supabase
    try:
        supabase.table("migration_sessions").delete().eq("id", session_id).execute()
        return {
            "status": "success",
            "messaggio": f"Sessione {session_id} e tutti i file correlati rimossi definitivamente.",
        }
    except Exception as e:
        logger.error("Errore eliminazione sessione %s dal DB: %s", session_id, e)
        raise HTTPException(status_code=500, detail="Errore durante l'eliminazione dal database.")
