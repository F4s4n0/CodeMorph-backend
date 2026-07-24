"""
Prova gratuita una tantum: l'admin concede il bonus, l'utente genera il
SOLO documento di Assessment su max 50 righe di codice, a spese della
piattaforma, senza licenza né token.

I log dell'attività REALE dell'agente vengono scritti su un file per utente
e letti dal frontend in polling durante l'elaborazione (stesso schema della
pipeline principale, ma senza sessione: la chiave è l'user_id del token).
"""
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from crewai import Crew, Process, Task
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import _parse_expiry, get_current_user, supabase
from payments import _verifica_admin
from src.agents import create_agents
from src.llm_config import get_llm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/trial")

MAX_RIGHE = 50
MAX_CARATTERI = 10_000  # 50 righe non possono valere 10k caratteri l'una

# Provider/modello della prova gratuita (a spese della piattaforma).
# Configurabili da env per cambiarli senza deploy.
TRIAL_PROVIDER = os.getenv("TRIAL_PROVIDER", "anthropic")
TRIAL_MODEL = os.getenv("TRIAL_MODEL", "claude-haiku-4-5-20251001")

WORKSPACE_DIR = Path(os.getenv("WORKSPACE_DIR", "/tmp/workspace_sessioni"))
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# =====================================================================
# Log dell'attività (file per utente, letto in polling dal frontend)
# =====================================================================

def _percorso_log(user_id):
    """File di log della prova. user_id validato: finisce dentro un percorso."""
    if not _ID_RE.match(user_id or ""):
        raise HTTPException(status_code=400, detail="Identificativo utente non valido.")
    return WORKSPACE_DIR / "trial_logs" / f"{user_id}.txt"


def _log(user_id, messaggio):
    """Accoda una riga al log della prova (best-effort: non deve mai bloccare)."""
    try:
        percorso = _percorso_log(user_id)
        percorso.parent.mkdir(parents=True, exist_ok=True)
        ora = datetime.now(timezone.utc).strftime("%H:%M:%S")
        with open(percorso, "a", encoding="utf-8") as f:
            f.write(f"[{ora}] {messaggio}\n")
    except Exception as e:
        logger.warning("Log trial non scritto per %s: %s", user_id, e)


def _azzera_log(user_id):
    try:
        percorso = _percorso_log(user_id)
        percorso.parent.mkdir(parents=True, exist_ok=True)
        open(percorso, "w", encoding="utf-8").close()
    except Exception as e:
        logger.warning("Log trial non azzerato per %s: %s", user_id, e)


@router.get("/logs")
def logs_trial(user_id: str = Depends(get_current_user)):
    """
    Log dell'analisi in corso per l'utente autenticato.
    Ognuno legge SOLO il proprio file: il percorso è costruito dall'user_id
    del token, mai da un parametro del client.
    """
    percorso = _percorso_log(user_id)
    if not percorso.exists():
        return {"logs": ""}
    try:
        with open(percorso, "r", encoding="utf-8") as f:
            return {"logs": f.read()}
    except Exception as e:
        logger.warning("Lettura log trial fallita per %s: %s", user_id, e)
        return {"logs": ""}


# =====================================================================
# Stato del bonus
# =====================================================================

def _ha_licenza_attiva(user_id):
    """True se l'utente ha un pass ancora valido: in quel caso niente trial."""
    try:
        r = (supabase.table("user_licenses").select("expires_at")
             .eq("user_id", user_id).order("expires_at", desc=True).limit(1).execute())
        if not r.data:
            return False
        return _parse_expiry(r.data[0]["expires_at"]) > datetime.now(timezone.utc)
    except Exception as e:
        logger.warning("Verifica licenza per trial fallita (%s): %s", user_id, e)
        return False


class InputTrial(BaseModel):
    codice: str


@router.get("/stato")
def stato_trial(user_id: str = Depends(get_current_user)):
    """disponibile = bonus concesso, non usato e SENZA licenza attiva."""
    licenza_attiva = _ha_licenza_attiva(user_id)
    try:
        r = supabase.table("trial_bonuses").select("used_at").eq("user_id", user_id).execute()
    except Exception as e:
        logger.error("Lettura trial fallita per %s: %s", user_id, e)
        raise HTTPException(status_code=503, detail="Servizio non disponibile.")
    if not r.data:
        return {"concesso": False, "disponibile": False, "usato": False, "licenza_attiva": licenza_attiva}
    usato = r.data[0]["used_at"] is not None
    return {
        "concesso": True,
        "disponibile": (not usato) and (not licenza_attiva),
        "usato": usato,
        "licenza_attiva": licenza_attiva,
    }


# =====================================================================
# Esecuzione della prova
# =====================================================================

@router.post("/esegui")
def esegui_trial(richiesta: InputTrial, user_id: str = Depends(get_current_user)):
    codice = (richiesta.codice or "").strip()
    righe = codice.splitlines()

    # Limiti verificati QUI: il frontend può essere aggirato, il server no
    if not codice:
        raise HTTPException(status_code=400, detail="Incolla il codice da analizzare.")
    if len(righe) > MAX_RIGHE:
        raise HTTPException(
            status_code=400,
            detail=f"La prova gratuita accetta al massimo {MAX_RIGHE} righe (ne hai incollate {len(righe)}).",
        )
    if len(codice) > MAX_CARATTERI:
        raise HTTPException(status_code=400, detail="Codice troppo lungo per la prova gratuita.")
    if _ha_licenza_attiva(user_id):
        raise HTTPException(
            status_code=400,
            detail="Hai già un pass attivo: usa la pipeline completa per analizzare il codice, la prova gratuita non è disponibile.",
        )

    # Consumo ATOMICO del bonus PRIMA di lanciare l'IA: due richieste in
    # parallelo non possono usarlo entrambe (update condizionato su used_at NULL)
    presa = (
        supabase.table("trial_bonuses")
        .update({"used_at": datetime.now(timezone.utc).isoformat(), "righe_codice": len(righe)})
        .eq("user_id", user_id)
        .is_("used_at", "null")
        .execute()
    )
    if not presa.data:
        raise HTTPException(
            status_code=403,
            detail="La prova gratuita non è disponibile per questo account o è già stata utilizzata.",
        )

    _azzera_log(user_id)
    _log(user_id, f"Avvio della prova gratuita su {len(righe)} righe di codice.")
    _log(user_id, f"Inizializzazione del Legacy System Analyzer ({TRIAL_MODEL}).")

    try:
        llm = get_llm(provider=TRIAL_PROVIDER, model_name=TRIAL_MODEL)
        agents = create_agents(llm)

        def step_callback(step):
            """Registra l'attività reale dell'agente, passo per passo."""
            try:
                testo = getattr(step, "thought", None) or getattr(step, "output", None) or str(step)
                testo = " ".join(str(testo).split())[:160]
                if testo:
                    _log(user_id, testo)
            except Exception:
                pass

        task = Task(
            description=(
                "IMPORTANTE: scrivi la tua risposta ESCLUSIVAMENTE IN LINGUA ITALIANA. "
                "Ogni titolo, paragrafo, tabella e commento deve essere in italiano.\n\n"
                "Analizza attentamente il seguente estratto di codice legacy "
                "(campione limitato fornito per una valutazione dimostrativa):\n\n"
                f'"""\n{codice}\n"""\n\n'
                "Esegui un'analisi statica approfondita: identifica strutture dati, "
                "logica di business, dipendenze, costanti e criticità dell'estratto."
            ),
            expected_output=(
                "Un documento di 'Inventory' scritto INTERAMENTE IN ITALIANO, in formato "
                "Markdown, che elenca gli asset identificati nell'estratto, con l'analisi "
                "statica iniziale. Non usare l'inglese in nessuna sezione del documento."
            ),
            agent=agents["legacy_system_analyzer"],
        )
        crew = Crew(
            agents=[agents["legacy_system_analyzer"]],
            tasks=[task],
            process=Process.sequential,
            verbose=False,
            memory=False,
            step_callback=step_callback,
        )

        _log(user_id, "Analisi statica del codice in corso...")
        risultato = crew.kickoff()
        documento = getattr(risultato, "raw", None) or str(risultato)
        _log(user_id, "Documento di Assessment generato con successo.")
        return {"status": "success", "documento": documento}
    except Exception:
        # LLM fallito: restituiamo il bonus, l'utente potrà riprovare
        logger.exception("Trial fallito per %s: bonus ripristinato.", user_id)
        _log(user_id, "Analisi non riuscita: il bonus e' stato ripristinato.")
        supabase.table("trial_bonuses").update({"used_at": None}).eq("user_id", user_id).execute()
        raise HTTPException(
            status_code=500,
            detail="Analisi non riuscita: il tuo bonus resta valido, riprova tra poco.",
        )


@router.post("/admin/{destinatario_id}/concedi")
def concedi_trial(destinatario_id: str, user_id: str = Depends(get_current_user)):
    """[ADMIN] Concede (o ri-concede, azzerando l'uso) il bonus a un account."""
    _verifica_admin(user_id)
    try:
        supabase.table("trial_bonuses").upsert({
            "user_id": destinatario_id, "granted_by": user_id,
            "granted_at": datetime.now(timezone.utc).isoformat(), "used_at": None,
        }).execute()
        return {"status": "success", "messaggio": "Bonus prova concesso."}
    except Exception as e:
        logger.error("Concessione trial fallita per %s: %s", destinatario_id, e)
        raise HTTPException(status_code=500, detail="Concessione non riuscita.")
