"""
Agenti e provvigioni.

L'attribuzione del cliente all'agente è PERMANENTE e avviene una sola
volta, alla registrazione. Ogni acquisto successivo — rinnovi compresi —
genera una provvigione: se il cliente continua a comprare, l'agente
continua a incassare.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_current_user, supabase
from payments import _verifica_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/affiliazione")


class InputAttribuzione(BaseModel):
    codice: str


@router.post("/attribuisci")
def attribuisci_agente(dati: InputAttribuzione, user_id: str = Depends(get_current_user)):
    """
    Collega l'account all'agente che l'ha segnalato. Irreversibile: se il
    profilo ha già un agente, la richiesta viene ignorata (nessuno può
    "rubare" un cliente già attribuito riproponendo il proprio codice).
    """
    codice = (dati.codice or "").strip()
    if not codice:
        raise HTTPException(status_code=400, detail="Codice mancante.")

    try:
        p = supabase.table("profiles").select("agente_id").eq("id", user_id).single().execute()
        if p.data and p.data.get("agente_id"):
            return {"status": "gia_attribuito"}

        a = (supabase.table("agenti").select("id,attivo")
             .eq("codice", codice).limit(1).execute())
        if not a.data or not a.data[0].get("attivo"):
            logger.info("Codice agente '%s' inesistente o disattivato.", codice)
            return {"status": "codice_non_valido"}

        supabase.table("profiles").update({
            "agente_id": a.data[0]["id"],
            "agente_attribuito_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", user_id).execute()
        logger.info("Utente %s attribuito all'agente %s.", user_id, codice)
        return {"status": "attribuito"}
    except Exception as e:
        logger.error("Attribuzione agente fallita per %s: %s", user_id, e)
        raise HTTPException(status_code=500, detail="Attribuzione non riuscita.")


def registra_provvigione(user_id, order_id, importo_ordine_eur):
    """
    Da chiamare quando un ordine viene EROGATO (non quando viene creato).

    Best-effort: un problema qui non deve mai bloccare l'erogazione al
    cliente — il compenso si può sempre ricostruire dagli ordini.
    """
    try:
        p = supabase.table("profiles").select("agente_id").eq("id", user_id).single().execute()
        agente_id = (p.data or {}).get("agente_id")
        # Il compenso spetta solo sugli acquisti entro 12 mesi dalla
        # registrazione del cliente: oltre, l'attribuzione resta ma non paga.
        attribuito = p.data.get("agente_attribuito_at")
        if attribuito:
            data_attr = datetime.fromisoformat(attribuito.replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - data_attr).days > 365:
                return None
        if not agente_id:
            return None                      # cliente diretto: nessuna provvigione

        a = (supabase.table("agenti").select("percentuale,attivo")
             .eq("id", agente_id).single().execute())
        if not a.data or not a.data.get("attivo"):
            return None

        percentuale = Decimal(str(a.data["percentuale"]))
        importo = (Decimal(str(importo_ordine_eur)) * percentuale / Decimal(100)).quantize(Decimal("0.01"))

        supabase.table("provvigioni").insert({
            "agente_id": agente_id,
            "user_id": user_id,
            "order_id": order_id,
            "importo_ordine_eur": float(importo_ordine_eur),
            "percentuale": float(percentuale),
            "importo_eur": float(importo),
            "stato": "maturata",
        }).execute()
        logger.info("Provvigione %.2f EUR maturata per l'agente %s (ordine %s).",
                    importo, agente_id, order_id)
        return importo
    except Exception as e:
        # UNIQUE su order_id: se scatta, la provvigione era già registrata
        logger.warning("Provvigione non registrata per l'ordine %s: %s", order_id, e)
        return None


@router.get("/admin/agenti")
def elenco_agenti(user_id: str = Depends(get_current_user)):
    """[ADMIN] Agenti con il riepilogo dei compensi maturati e liquidati."""
    _verifica_admin(user_id)
    agenti = supabase.table("agenti").select("*").order("nome").execute().data or []
    prov = supabase.table("provvigioni").select("agente_id,importo_eur,stato").execute().data or []

    for ag in agenti:
        mie = [p for p in prov if p["agente_id"] == ag["id"]]
        ag["clienti_attribuiti"] = len({p.get("user_id") for p in mie})
        ag["maturato_eur"] = round(sum(float(p["importo_eur"]) for p in mie if p["stato"] == "maturata"), 2)
        ag["liquidato_eur"] = round(sum(float(p["importo_eur"]) for p in mie if p["stato"] == "liquidata"), 2)
    return agenti


@router.get("/admin/provvigioni")
def elenco_provvigioni(user_id: str = Depends(get_current_user)):
    """[ADMIN] Tutte le provvigioni, dalla più recente."""
    _verifica_admin(user_id)
    r = (supabase.table("provvigioni").select("*")
         .order("created_at", desc=True).limit(200).execute())
    return r.data or []


@router.post("/admin/provvigioni/{provvigione_id}/liquida")
def liquida_provvigione(provvigione_id: str, user_id: str = Depends(get_current_user)):
    """[ADMIN] Segna una provvigione come pagata all'agente."""
    _verifica_admin(user_id)
    r = (supabase.table("provvigioni")
         .update({"stato": "liquidata", "liquidata_at": datetime.now(timezone.utc).isoformat()})
         .eq("id", provvigione_id).eq("stato", "maturata").execute())
    if not r.data:
        raise HTTPException(status_code=400, detail="Provvigione inesistente o già liquidata.")
    return {"status": "liquidata"}