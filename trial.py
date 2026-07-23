"""
Prova gratuita una tantum: l'admin concede il bonus, l'utente genera il
SOLO documento di Assessment su max 50 righe di codice, a spese della
piattaforma (Gemini Flash con la chiave server), senza licenza né token.
"""
import logging
from datetime import datetime, timezone

from crewai import Crew, Process, Task
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_current_user, supabase
from payments import _verifica_admin
from src.agents import create_agents
from src.llm_config import get_llm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/trial")

MAX_RIGHE = 50
MAX_CARATTERI = 10_000  # 50 righe non possono valere 10k caratteri l'una


class InputTrial(BaseModel):
    codice: str


@router.get("/stato")
def stato_trial(user_id: str = Depends(get_current_user)):
    """disponibile = bonus concesso e non ancora usato."""
    try:
        r = supabase.table("trial_bonuses").select("used_at").eq("user_id", user_id).execute()
    except Exception as e:
        logger.error("Lettura trial fallita per %s: %s", user_id, e)
        raise HTTPException(status_code=503, detail="Servizio non disponibile.")
    if not r.data:
        return {"concesso": False, "disponibile": False, "usato": False}
    usato = r.data[0]["used_at"] is not None
    return {"concesso": True, "disponibile": not usato, "usato": usato}


@router.post("/esegui")
def esegui_trial(richiesta: InputTrial, user_id: str = Depends(get_current_user)):
    codice = (richiesta.codice or "").strip()
    righe = codice.splitlines()

    # Limiti verificati QUI: il frontend può essere aggirato, il server no
    if not codice:
        raise HTTPException(status_code=400, detail="Incolla il codice da analizzare.")
    if len(righe) > MAX_RIGHE:
        raise HTTPException(status_code=400, detail=f"La prova gratuita accetta al massimo {MAX_RIGHE} righe (ne hai incollate {len(righe)}).")
    if len(codice) > MAX_CARATTERI:
        raise HTTPException(status_code=400, detail="Codice troppo lungo per la prova gratuita.")

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
        raise HTTPException(status_code=403, detail="La prova gratuita non è disponibile per questo account o è già stata utilizzata.")

    try:
        llm = get_llm(provider="google", model_name="gemini-3.5-flash")
        agents = create_agents(llm)
        task = Task(
            description=(
                "Analizza attentamente il seguente estratto di codice legacy "
                "(campione limitato fornito per una valutazione dimostrativa):\n\n"
                f'"""\n{codice}\n"""\n\n'
                "Esegui un'analisi statica approfondita: identifica strutture dati, "
                "logica di business, dipendenze, costanti e criticità dell'estratto."
            ),
            expected_output=(
                "Un documento di 'Inventory' in Markdown che elenca gli asset "
                "identificati nell'estratto, con l'analisi statica iniziale."
            ),
            agent=agents["legacy_system_analyzer"],
        )
        crew = Crew(agents=[agents["legacy_system_analyzer"]], tasks=[task],
                    process=Process.sequential, verbose=False, memory=False)
        risultato = crew.kickoff()
        documento = getattr(risultato, "raw", None) or str(risultato)
        return {"status": "success", "documento": documento}
    except Exception:
        # LLM fallito: restituiamo il bonus, l'utente potrà riprovare
        logger.exception("Trial fallito per %s: bonus ripristinato.", user_id)
        supabase.table("trial_bonuses").update({"used_at": None}).eq("user_id", user_id).execute()
        raise HTTPException(status_code=500, detail="Analisi non riuscita: il tuo bonus resta valido, riprova tra poco.")


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